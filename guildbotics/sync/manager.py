"""The one subscriber of the Workspace Sync Port.

Storage layers announce a completed shared write and carry on; this manager
turns those announcements into commits, fetches, automatic convergence, and
pushes on a single queue per device. Two behaviours, and only two, are visible
from outside: saving is asynchronous, and an execution boundary that must not
run twice can wait for a push with :meth:`GitSyncManager.await_pushed`.

Concurrent updates settle without asking the user anything. The hub accepts
fast-forwards only, so the change that reaches it first wins; a local commit
touching the same paths is stashed under a rejected ref on this device, the
non-overlapping part of it is reapplied, and one activity event records where
the stashed commit can be found. Wall clock, mtime, and three-way content
merges are never consulted.

Damage is the one thing that is not settled automatically. A record whose
identifier was minted twice, a shared file that does not validate, another
workspace's data, or a broken repository stop the queue for diagnosis instead
of being overwritten.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from git import GitCommandError

from guildbotics.sync.commits import (
    UnsendableChange,
    commit_shared_changes,
    validate_received,
)
from guildbotics.sync.local_repository import LocalSyncRepository, SyncRepositoryError
from guildbotics.sync.rejections import RejectionRecorder, record_update_rejected
from guildbotics.utils.shared_write_lock import (
    SharedWriteBusyError,
    shared_write_lock,
)
from guildbotics.utils.sync_lock import SyncRepositoryBusyError, sync_repository_lock
from guildbotics.utils.timestamps import utc_now_iso
from guildbotics.utils.workspace_sync_port import ChangeSet
from guildbotics.workspace.identity import (
    WorkspaceIdentity,
    ensure_device_identity,
    ensure_workspace_identity,
    new_uuid7,
)
from guildbotics.workspace.validation import (
    SharedFileInvalidError,
    SharedSchemaAheadError,
    validate_shared_file,
)

SyncState = Literal[
    "idle",
    "fetching",
    "reconciling",
    "pushing",
    "unreachable",
    "invalid_shared_state",
    "update_required",
]

#: Directories whose files are written once under a generated identifier. Two
#: devices creating the same path there means an identifier was minted twice,
#: which is damage rather than a concurrent edit of one file.
IMMUTABLE_SHARED_PREFIXES = ("state/events/",)
#: The hub can move again between the decision and the push; give way and redo
#: the same decision a bounded number of times rather than looping forever.
MAX_PUSH_ATTEMPTS = 5
#: How long a device waits for the hub before checking on its own (§7.2).
FALLBACK_INTERVAL_SECONDS = 60.0
#: A burst of saves becomes one commit when they arrive within this window.
COALESCE_SECONDS = 0.2
#: How long an execution barrier waits for its change to reach the hub.
PUSH_BARRIER_SECONDS = 120.0
#: Settled changes are remembered for a while so a barrier that asks just after
#: its change was pushed still gets an answer instead of a bare "unknown".
MAX_TRACKED_CHANGES = 1000

_WORKSPACE_IDENTITY_PATH = "state/workspace.json"

LOGGER = logging.getLogger(__name__)


class SharedDataAnomaly(RuntimeError):
    """Raised when shared data is damaged rather than merely out of date.

    Attributes:
        code (str): A stable identifier for the kind of damage.
        state (SyncState): The state the queue stops in.
    """

    def __init__(
        self, code: str, detail: str, *, state: SyncState = "invalid_shared_state"
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.state = state


@dataclass(frozen=True)
class GitSyncStatus:
    """What synchronization exposes to the rest of the system.

    This is display state, never persisted: after a restart it is recomputed
    from the repository itself.
    """

    workspace_id: str
    state: SyncState
    local_head: str | None
    remote_head: str | None
    ahead_count: int
    behind_count: int
    invalid_paths: tuple[UnsendableChange, ...]
    last_success_at: str | None
    last_error_code: str | None


@dataclass
class _Pending:
    """One announced change waiting to reach the hub."""

    change: ChangeSet
    done: threading.Event = field(default_factory=threading.Event)
    settled: bool = False
    shared: bool = False


class GitSyncManager:
    """Synchronizes one workspace's shared state with its hub.

    Args:
        repository (LocalSyncRepository): The workspace's own repository.
        workspace_id (str): The workspace this manager refuses to mix with others.
        device_id (str): This machine, named in rejection events and commits.
        record_rejection (RejectionRecorder): Records a rejected local change.
        fallback_interval (float): Seconds between unprompted hub checks.
        coalesce_delay (float): Seconds a burst of saves is gathered before commit.
        push_barrier_timeout (float): Seconds :meth:`await_pushed` waits.
        max_push_attempts (int): How often a lost race is redone before giving up.
    """

    def __init__(
        self,
        repository: LocalSyncRepository,
        *,
        workspace_id: str,
        device_id: str,
        record_rejection: RejectionRecorder = record_update_rejected,
        fallback_interval: float = FALLBACK_INTERVAL_SECONDS,
        coalesce_delay: float = COALESCE_SECONDS,
        push_barrier_timeout: float = PUSH_BARRIER_SECONDS,
        max_push_attempts: int = MAX_PUSH_ATTEMPTS,
    ) -> None:
        self._repository = repository
        self._workspace_id = workspace_id
        self._device_id = device_id
        self._record_rejection = record_rejection
        self._fallback_interval = fallback_interval
        self._coalesce_delay = coalesce_delay
        self._push_barrier_timeout = push_barrier_timeout
        self._max_push_attempts = max_push_attempts

        self._state: SyncState = "idle"
        self._invalid_paths: tuple[UnsendableChange, ...] = ()
        self._last_success_at: str | None = None
        self._last_error_code: str | None = None

        self._pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._sync_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None

    # -- Workspace Sync Port ------------------------------------------------

    def shared_state_changed(self, change: ChangeSet) -> bool:
        """Queue an announced shared write, returning whether it was accepted."""
        if self._halted():
            return False
        with self._pending_lock:
            self._pending.setdefault(change.change_id, _Pending(change=change))
        self._wake.set()
        return True

    def wake(self) -> None:
        """Wake the queue for a Hub head hint without changing its contract."""
        self._wake.set()

    def await_pushed(self, change_id: str) -> bool:
        """Block until a commit containing ``change_id`` reaches the hub.

        Returns:
            bool: True only when the change is durably shared. A change the hub
                did not accept, damaged shared data, and an unreachable hub all
                return False, so an execution boundary never treats an
                unconfirmed change as confirmed.
        """
        with self._pending_lock:
            pending = self._pending.get(change_id)
        if pending is None:
            # Either the change was never announced, or it settled so long ago
            # that it has been forgotten. Neither is a confirmation.
            return False
        self._wake.set()
        if not pending.done.wait(self._push_barrier_timeout):
            return False
        return pending.shared

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Run the device's single synchronization queue in the background.

        Returns:
            bool: False when a worker is still running, which is the case after
                a :meth:`stop` that timed out. Starting a second one would put
                two threads on one repository, so the caller is told instead.
        """
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            # Each worker watches an event of its own, so a thread that outlives
            # its stop can never be revived by the next start.
            stopping = threading.Event()
            self._stopping = stopping
            self._worker = threading.Thread(
                target=self._serve,
                args=(stopping,),
                name="guildbotics-sync",
                daemon=True,
            )
            self._worker.start()
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop the background queue, leaving unsent commits for the next start.

        Returns:
            bool: Whether the worker actually finished. A fetch or push can
                block for longer than ``timeout``; until it returns, the worker
                is kept so no second one can be started beside it.
        """
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return True
            self._stopping.set()
            self._wake.set()
            worker.join(timeout)
            if worker.is_alive():
                return False
            self._worker = None
            return True

    def _serve(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            notified = self._wake.wait(self._fallback_interval)
            self._wake.clear()
            if stopping.is_set():
                return
            if notified and self._coalesce_delay > 0:
                time.sleep(self._coalesce_delay)
                self._wake.clear()
            try:
                self.synchronize()
            except Exception:
                # Every failure synchronization expects is already turned into
                # a status by ``synchronize``. Anything left is a defect, and a
                # worker that dies of one leaves a device that looks idle while
                # it has stopped synchronizing -- so the queue keeps going and
                # says why. Direct callers still see the exception.
                LOGGER.exception("The synchronization queue hit an unexpected error.")
                self._last_error_code = "unexpected_error"

    # -- Synchronization ----------------------------------------------------

    def synchronize(self) -> GitSyncStatus:
        """Send, receive, and converge once, reporting the resulting status."""
        try:
            with sync_repository_lock(self._repository.workspace_root), self._sync_lock:
                return self._synchronize_status()
        except SyncRepositoryBusyError:
            # A one-shot member command is allowed to own the repository for
            # the duration of its push. The queue simply retries next cycle.
            self._last_error_code = "sync_busy"
            return self.status()

    def commit_and_push_once(self, *, timeout: float | None = None) -> GitSyncStatus:
        """Commit and make one push attempt without fetching or converging.

        This is the member CLI's one-shot synchronization boundary. It runs
        under the same process-wide lock as the background queue, commits all
        currently sendable shared changes, and makes at most one push attempt.
        A hub race or outage is left for the next queue cycle; the returned
        status records whether this one-shot reached the hub. A local lock
        timeout is raised so the caller cannot report an uncoordinated write
        as successful.
        """
        with (
            sync_repository_lock(self._repository.workspace_root, timeout=timeout),
            self._sync_lock,
        ):
            try:
                self._repository.verify_boundary()
                self._verify_local_identity()
                self._commit_working_tree()
                if not self._repository.has_remote():
                    self._state = "unreachable"
                    self._last_error_code = "hub_not_configured"
                    return self.status()
                self._state = "pushing"
                self._repository.push()
            except SharedDataAnomaly as anomaly:
                self._halt(anomaly)
                return self.status()
            except SharedWriteBusyError:
                self._last_error_code = "local_write_busy"
                return self.status()
            except (GitCommandError, SyncRepositoryError, OSError) as exc:
                self._state = "unreachable"
                self._last_error_code = type(exc).__name__
                return self.status()
            self._state = "idle"
            self._last_error_code = None
            self._last_success_at = utc_now_iso()
            self._resolve_shared()
            return self.status()

    def resume(self) -> GitSyncStatus:
        """Clear a stop caused by damaged shared data and try again.

        Damage is not retried on its own; the user repairs it and asks for
        another attempt.
        """
        with sync_repository_lock(self._repository.workspace_root), self._sync_lock:
            if self._halted():
                self._state = "idle"
                self._last_error_code = None
            return self._synchronize_status()

    def _synchronize_status(self) -> GitSyncStatus:
        """Run one cycle with both outer locks already held."""
        if self._halted():
            return self.status()
        try:
            self._synchronize()
        except SharedDataAnomaly as anomaly:
            self._halt(anomaly)
        except SharedWriteBusyError:
            # A save is holding the workspace's files. Nothing is wrong with
            # the hub, and the next cycle picks the work up, so the state stays
            # as it was rather than claiming unreachable.
            self._last_error_code = "local_write_busy"
        except (GitCommandError, SyncRepositoryError, OSError) as exc:
            self._state = "unreachable"
            self._last_error_code = type(exc).__name__
        return self.status()

    def status(self) -> GitSyncStatus:
        """Report the current queue state, recomputed from the repository."""
        local = remote = None
        ahead = behind = 0
        try:
            local = self._repository.head()
            remote = self._repository.remote_head()
            if local is not None and remote is not None:
                ahead, behind = self._repository.ahead_behind(local, remote)
        except (GitCommandError, SyncRepositoryError, OSError):
            pass
        return GitSyncStatus(
            workspace_id=self._workspace_id,
            state=self._state,
            local_head=local,
            remote_head=remote,
            ahead_count=ahead,
            behind_count=behind,
            invalid_paths=self._invalid_paths,
            last_success_at=self._last_success_at,
            last_error_code=self._last_error_code,
        )

    def _synchronize(self) -> None:
        self._repository.verify_boundary()
        self._verify_local_identity()
        self._commit_working_tree()
        if not self._repository.has_remote():
            self._state = "unreachable"
            self._last_error_code = "hub_not_configured"
            return
        for _ in range(self._max_push_attempts):
            if self._synchronize_once():
                self._state = "idle"
                self._last_error_code = None
                self._last_success_at = utc_now_iso()
                self._resolve_shared()
                return
        self._state = "idle"
        self._last_error_code = "push_retry_exhausted"

    def _synchronize_once(self) -> bool:
        """Run one fetch, converge, and push. True when local state is shared."""
        self._state = "fetching"
        self._repository.fetch()
        remote = self._repository.remote_head()
        self._verify_workspace_identity(remote)
        local = self._repository.head()
        if remote is not None and remote != local:
            local = self._converge(local, remote)
        if local == remote:
            return True
        return self._push()

    def _converge(self, local: str | None, remote: str) -> str | None:
        """Adopt the hub's content, reapplying what does not collide with it.

        Held under the shared-write lock from end to end. Nothing here waits on
        the hub -- the fetch already happened -- so there is no reason to give
        the lock up part-way, and every reason not to: a writer let in between
        the commit and the checkout has its work taken away by the checkout
        with nothing recording the loss, and one let in between the checkout
        and the commit has its write committed as content it never saw.
        """
        with shared_write_lock(self._repository.workspace_root):
            # Everything since the last commit boundary ran without the lock,
            # because that interval waits on the hub. A save made there holds
            # the lock correctly and is still only in the working tree, so the
            # checkout below would take it away -- the one case a writer
            # holding the lock cannot protect itself from. Committing it first
            # turns it into a change with a name: it either survives the
            # adoption or collides and is rejected on the record. That also
            # makes `local` stale, so the cycle is redone against the new head
            # rather than reconciled from a state that no longer exists.
            self._commit_held()
            committed_in_the_interval = self._repository.head()
            if committed_in_the_interval != local:
                return committed_in_the_interval
            if local is None:
                base = None
            else:
                base = self._repository.merge_base(local, remote)
                if base is None:
                    raise SharedDataAnomaly(
                        "unrelated_histories",
                        f"{remote} shares no history with this workspace",
                    )
                if base == remote:
                    return local
            self._state = "reconciling"
            base_or_empty = base if base is not None else _EMPTY_TREE
            local_changes = (
                self._repository.changed_paths(base_or_empty, local)
                if local is not None
                else {}
            )
            remote_changes = self._repository.changed_paths(base_or_empty, remote)
            self._validate_received(remote, remote_changes)
            conflicts = set(local_changes) & set(remote_changes)
            if conflicts and local is not None:
                self._reject(local, sorted(conflicts), local_changes, remote_changes)
            self._repository.move_to(remote)
            # A change held back by validation was never shareable, so it is
            # not a rejection -- but it is still the user's work, and adopting
            # the hub's version over it would discard an edit they were told to
            # go and fix.
            held = {item.path for item in self._invalid_paths}
            self._repository.restore_from_index(sorted(set(remote_changes) - held))
            self._commit_held()
            return self._repository.head()

    def _reject(
        self,
        local: str,
        conflicts: list[str],
        local_changes: dict[str, str],
        remote_changes: dict[str, str],
    ) -> None:
        """Stash the losing commit and record where it can be found."""
        collisions = [
            path
            for path in conflicts
            if local_changes[path] == "A"
            and remote_changes[path] == "A"
            and path.startswith(IMMUTABLE_SHARED_PREFIXES)
        ]
        if collisions:
            raise SharedDataAnomaly(
                "immutable_id_collision",
                f"two devices created {', '.join(collisions)} with different content",
            )
        already = self._repository.rejected_id_for(local)
        self._reject_pending(conflicts)
        if already is not None:
            return
        rejection_id = new_uuid7()
        self._repository.save_rejected(rejection_id, local)
        self._record_rejection(
            rejection_id=rejection_id,
            paths=conflicts,
            device_id=self._device_id,
            workspace_id=self._workspace_id,
            workspace_root=self._repository.workspace_root,
        )

    def _push(self) -> bool:
        """Push the sync branch. False when the hub moved and the race is redone."""
        self._state = "pushing"
        try:
            self._repository.push()
        except GitCommandError as exc:
            # A push can succeed and still fail to report it. The hub's own
            # head decides, so a lost response never creates a second commit.
            self._repository.fetch()
            if self._repository.remote_head() == self._repository.head():
                return True
            if "non-fast-forward" in str(exc) or "fetch first" in str(exc):
                return False
            raise
        return True

    # -- Commit boundary ----------------------------------------------------

    def _commit_working_tree(self) -> None:
        """Commit every shared change that validates, holding back the rest."""
        with shared_write_lock(self._repository.workspace_root):
            self._commit_held()

    def _commit_held(self) -> None:
        """Commit, with the workspace's shared-write lock already held.

        A commit reads the working tree as one state, so a config save must not
        be halfway through writing its files while this runs -- the commit
        would carry part of that save and the next one the rest.
        """
        outcome = commit_shared_changes(self._repository, device_id=self._device_id)
        self._invalid_paths = outcome.unsendable

    # -- Received content ---------------------------------------------------

    def _verify_local_identity(self) -> None:
        """Refuse to send anything from a copy that lost its workspace identity.

        The identity is the only thing that stops two workspaces from being
        mixed, so a deletion of it must never become a change to share: once
        propagated, no device could tell the workspaces apart again. A
        repository with no commits yet is exempt, because that is the state a
        device is in while it is still taking the workspace from the hub.
        """
        if self._repository.head() is None:
            return
        path = (
            self._repository.workspace_root
            / ".guildbotics"
            / (_WORKSPACE_IDENTITY_PATH)
        )
        if not path.is_file():
            raise SharedDataAnomaly(
                "missing_workspace_identity",
                f"{_WORKSPACE_IDENTITY_PATH} is gone from this workspace",
            )
        identity = self._read_identity(_WORKSPACE_IDENTITY_PATH, path.read_bytes())
        if identity.workspace_id != self._workspace_id:
            raise SharedDataAnomaly(
                "workspace_identity_mismatch",
                f"this copy now holds workspace {identity.workspace_id}, "
                f"not {self._workspace_id}",
            )

    def _read_identity(self, path: str, data: bytes) -> WorkspaceIdentity:
        """Parse a workspace identity, reporting damage as damage.

        Both sides parse through here so an identity this build cannot read
        stops the queue where it can be diagnosed. Letting the parse error
        escape instead would end the background worker, leaving a device that
        looks idle while it is no longer synchronizing at all.
        """
        try:
            validate_shared_file(path, data)
            return WorkspaceIdentity.model_validate_json(data)
        except SharedFileInvalidError as exc:
            raise self._anomaly_for(exc) from exc
        except ValueError as exc:
            raise SharedDataAnomaly("invalid_shared_file", f"{path}: {exc}") from exc

    def _verify_workspace_identity(self, remote: str | None) -> None:
        """Refuse to synchronize with a hub holding a different workspace."""
        if remote is None:
            return
        data = self._repository.read_blob(remote, _WORKSPACE_IDENTITY_PATH)
        if data is None:
            raise SharedDataAnomaly(
                "missing_workspace_identity",
                f"the hub has commits but no {_WORKSPACE_IDENTITY_PATH}",
            )
        identity = self._read_identity(_WORKSPACE_IDENTITY_PATH, data)
        if identity.workspace_id != self._workspace_id:
            raise SharedDataAnomaly(
                "workspace_identity_mismatch",
                f"the hub holds workspace {identity.workspace_id}, "
                f"not {self._workspace_id}",
            )

    def _validate_received(self, remote: str, changes: dict[str, str]) -> None:
        """Stop the queue on an arriving file this build cannot read."""
        try:
            validate_received(
                self._repository,
                remote,
                (path for path, status in changes.items() if status != "D"),
            )
        except SharedFileInvalidError as exc:
            raise self._anomaly_for(exc) from exc

    def _anomaly_for(self, exc: SharedFileInvalidError) -> SharedDataAnomaly:
        """Tell "this build is too old" apart from "this file is damaged"."""
        if isinstance(exc, SharedSchemaAheadError):
            return SharedDataAnomaly(
                "schema_version_ahead", str(exc), state="update_required"
            )
        return SharedDataAnomaly("invalid_shared_file", str(exc))

    # -- Pending changes ----------------------------------------------------

    def _resolve_shared(self) -> None:
        held = {item.path for item in self._invalid_paths}
        self._settle(lambda change: not held.intersection(change.paths), shared=True)

    def _reject_pending(self, conflicts: Sequence[str]) -> None:
        rejected = set(conflicts)
        self._settle(
            lambda change: bool(rejected.intersection(change.paths)), shared=False
        )

    def _halt(self, anomaly: SharedDataAnomaly) -> None:
        self._state = anomaly.state
        self._last_error_code = anomaly.code
        # Nothing more will be pushed until the damage is repaired, so waiting
        # barriers are told now instead of timing out one by one.
        self._settle(lambda change: True, shared=False)

    def _settle(self, matches: Callable[[ChangeSet], bool], *, shared: bool) -> None:
        with self._pending_lock:
            settling = [
                pending
                for pending in self._pending.values()
                if not pending.settled and matches(pending.change)
            ]
            for pending in settling:
                pending.shared = shared
                pending.settled = True
            self._forget_oldest_settled()
        for pending in settling:
            pending.done.set()

    def _forget_oldest_settled(self) -> None:
        excess = len(self._pending) - MAX_TRACKED_CHANGES
        if excess <= 0:
            # Below the cap nothing is forgotten. Slicing by a negative excess
            # would instead drop all but the newest few, and a barrier asking
            # about its own change would be told it is unknown.
            return
        settled = [
            change_id for change_id, pending in self._pending.items() if pending.settled
        ]
        for change_id in settled[:excess]:
            del self._pending[change_id]

    def _halted(self) -> bool:
        return self._state in {"invalid_shared_state", "update_required"}


def build_git_sync_manager(workspace_root: Path | None = None) -> GitSyncManager:
    """Create the manager for a workspace, minting its identities on first use."""
    return GitSyncManager(
        LocalSyncRepository(workspace_root),
        workspace_id=ensure_workspace_identity(workspace_root).workspace_id,
        device_id=ensure_device_identity().device_id,
    )


#: Git's empty tree, used as the base when this workspace has no commits yet.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
