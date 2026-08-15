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

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from git import GitCommandError

from guildbotics.sync.local_repository import (
    LocalSyncRepository,
    SyncRepositoryError,
    WorkingTreeChange,
)
from guildbotics.sync.rejections import RejectionRecorder, record_update_rejected
from guildbotics.utils.shared_file_validators import SharedFileInvalidError
from guildbotics.utils.workspace_sync_port import ChangeSet
from guildbotics.workspace.identity import (
    SHARED_RECORD_SCHEMA_VERSION,
    WorkspaceIdentity,
    ensure_device_identity,
    ensure_workspace_identity,
    new_uuid7,
)
from guildbotics.workspace.validation import validate_shared_file

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
class UnsendableChange:
    """A local change held back because the file does not validate.

    Attributes:
        path (str): The path relative to ``.guildbotics/``.
        reason (str): Why it was held back, for the user to act on.
    """

    path: str
    reason: str


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

    def start(self) -> None:
        """Run the device's single synchronization queue in the background."""
        if self._worker is not None:
            return
        self._stopping.clear()
        self._worker = threading.Thread(
            target=self._serve, name="guildbotics-sync", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background queue, leaving unsent commits for the next start."""
        self._stopping.set()
        self._wake.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout)

    def _serve(self) -> None:
        while not self._stopping.is_set():
            notified = self._wake.wait(self._fallback_interval)
            self._wake.clear()
            if self._stopping.is_set():
                return
            if notified and self._coalesce_delay > 0:
                time.sleep(self._coalesce_delay)
                self._wake.clear()
            self.synchronize()

    # -- Synchronization ----------------------------------------------------

    def synchronize(self) -> GitSyncStatus:
        """Send, receive, and converge once, reporting the resulting status."""
        with self._sync_lock:
            if self._halted():
                return self.status()
            try:
                self._synchronize()
            except SharedDataAnomaly as anomaly:
                self._halt(anomaly)
            except (GitCommandError, SyncRepositoryError, OSError) as exc:
                self._state = "unreachable"
                self._last_error_code = type(exc).__name__
            return self.status()

    def resume(self) -> GitSyncStatus:
        """Clear a stop caused by damaged shared data and try again.

        Damage is not retried on its own; the user repairs it and asks for
        another attempt.
        """
        with self._sync_lock:
            if self._halted():
                self._state = "idle"
                self._last_error_code = None
            return self.synchronize()

    def change_remote(self, url: str) -> GitSyncStatus:
        """Point this workspace at a different hub and synchronize with it."""
        with self._sync_lock:
            self._repository.verify_boundary()
            self._repository.set_remote(url)
            if self._halted():
                self._state = "idle"
                self._last_error_code = None
        return self.synchronize()

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
        self._commit_working_tree()
        if not self._repository.has_remote():
            self._state = "unreachable"
            self._last_error_code = "hub_not_configured"
            return
        for _ in range(self._max_push_attempts):
            if self._synchronize_once():
                self._state = "idle"
                self._last_error_code = None
                self._last_success_at = _utc_now()
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
        """Adopt the hub's content, reapplying what does not collide with it."""
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
        # A change held back by validation was never shareable, so it is not a
        # rejection -- but it is still the user's work, and adopting the hub's
        # version over it would discard an edit they were told to go and fix.
        held = {item.path for item in self._invalid_paths}
        self._repository.restore_from_index(sorted(set(remote_changes) - held))
        self._commit_working_tree()
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
        """Commit every shared change that validates, holding back the rest.

        This is also what recovers a change whose save notification was lost
        and what picks up an edit made with an external editor: the scan looks
        at the working tree, not at what was announced.
        """
        sendable: list[WorkingTreeChange] = []
        held: list[UnsendableChange] = []
        for change in self._repository.working_tree_changes():
            if change.deleted:
                sendable.append(change)
                continue
            try:
                validate_shared_file(
                    change.path, self._repository.read_working_tree(change.path)
                )
            except SharedFileInvalidError as exc:
                held.append(UnsendableChange(path=change.path, reason=exc.reason))
            else:
                sendable.append(change)
        self._invalid_paths = tuple(held)
        if not sendable:
            return
        self._repository.stage([change.path for change in sendable])
        self._repository.commit(self._commit_message(sendable))

    def _commit_message(self, changes: Sequence[WorkingTreeChange]) -> str:
        deleted = sum(1 for change in changes if change.deleted)
        return (
            f"Sync shared state: {len(changes) - deleted} written, {deleted} deleted"
            f"\n\nDevice: {self._device_id}\nRecorded-At: {_utc_now()}\n"
        )

    # -- Received content ---------------------------------------------------

    def _verify_workspace_identity(self, remote: str | None) -> None:
        """Refuse to synchronize with a hub holding a different workspace."""
        if remote is None:
            return
        data = self._repository.read_blob(remote, _WORKSPACE_IDENTITY_PATH)
        if data is None:
            return
        try:
            identity = WorkspaceIdentity.model_validate_json(data)
        except ValueError as exc:
            raise self._anomaly_for(_WORKSPACE_IDENTITY_PATH, data, str(exc)) from exc
        if identity.workspace_id != self._workspace_id:
            raise SharedDataAnomaly(
                "workspace_identity_mismatch",
                f"the hub holds workspace {identity.workspace_id}, "
                f"not {self._workspace_id}",
            )

    def _validate_received(self, remote: str, changes: dict[str, str]) -> None:
        """Check arriving files with the same validation the send side used.

        Only validated content is ever sent, so a file that fails here means a
        defect or a damaged repository -- never a mistake the user made.
        """
        for path, status in changes.items():
            if status == "D":
                continue
            data = self._repository.read_blob(remote, path)
            if data is None:
                continue
            try:
                validate_shared_file(path, data)
            except SharedFileInvalidError as exc:
                raise self._anomaly_for(path, data, exc.reason) from exc

    def _anomaly_for(self, path: str, data: bytes, reason: str) -> SharedDataAnomaly:
        """Tell "this build is too old" apart from "this file is damaged"."""
        if _declared_schema_version(data) > SHARED_RECORD_SCHEMA_VERSION:
            return SharedDataAnomaly(
                "schema_version_ahead",
                f"{path} was written by a newer GuildBotics",
                state="update_required",
            )
        return SharedDataAnomaly("invalid_shared_file", f"{path} {reason}")

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


def _declared_schema_version(data: bytes) -> int:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    return version if isinstance(version, int) else 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
