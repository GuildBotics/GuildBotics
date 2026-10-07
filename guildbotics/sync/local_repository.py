"""The workspace's own Git repository, and the boundary that keeps it its own.

``<workspace>/.guildbotics`` is initialized as an independent repository so the
synchronized assets never mix with the user's work repositories: member working
clones live under the ignored ``local/clones/``, and their branches, uncommitted
edits, stashes, and remotes stay untouched by synchronization.

The repository path is derived from the selected workspace root on every call
rather than accepted from a caller. A path handed in by a capability or a
command could name a member working clone, and committing there would rewrite
the user's own work; deriving and re-verifying it makes that unrepresentable.

Only the mechanics live here. What a change means, when to send it, and how to
settle a concurrent update belong to
:class:`~guildbotics.sync.manager.GitSyncManager`.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from git import Git, GitCommandError, Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from guildbotics.utils.fileio import (
    ATOMIC_WRITE_SUFFIX,
    atomic_write_text,
    get_workspace_root,
)
from guildbotics.utils.openssh import (
    REMOTE_COMMAND_TIMEOUT_SECONDS,
    OpenSshNotFoundError,
    git_ssh_command,
)
from guildbotics.utils.workspace_sync_port import SHARED_ROOTS
from guildbotics.workspace.validation import REGULAR_FILE_MODES

#: The only branch normal operation shares. Users are given no branch controls.
SYNC_BRANCH = "main"
#: The hub remote. A workspace without it is initialized but not yet connected.
SYNC_REMOTE = "origin"
#: Where a local commit goes when the hub already accepted a change to its paths.
REJECTED_REF_PREFIX = "refs/guildbotics/rejected"
#: Where a hub's content is read before the workspace is connected to it.
PREVIEW_REF = "refs/guildbotics/hub-preview"
#: ``local/`` is device-only. Hidden paths are not GuildBotics shared data;
#: ignoring the whole category keeps OS and editor bookkeeping out at every
#: depth and also refuses ``.env`` a second time. The final entry is an atomic
#: write in progress: it is created beside its
#: destination, so for a shared file it lands inside this tree, and a cycle
#: that enumerates it either commits a half-written name or fails its own
#: ``git add`` when the rename beats it -- reported as a hub it could not
#: reach. It is never a file the user meant to share.
GITIGNORE_CONTENT = f"local/\n.*\n*{ATOMIC_WRITE_SUFFIX}\n"
#: Attributes that switch off every conversion between the index and disk.
GIT_ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"
#: Git's empty tree: the base of a comparison with a side that has no commits.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
#: Command lines stay bounded when a rescan finds thousands of changed files.
_PATH_BATCH = 200
#: ``git status --porcelain`` prefixes every entry with ``XY `` before the path.
_STATUS_PREFIX = 3


class SyncRepositoryError(RuntimeError):
    """Raised when the local synchronization repository cannot be operated."""


class HubTimeoutError(SyncRepositoryError):
    """Raised when a Git command that reaches the hub does not answer in time."""


class HubCommandError(SyncRepositoryError):
    """Raised when a Git command that reaches the hub fails.

    The message is what Git, ssh, and the hub printed, exactly as printed. It
    is the only place the reason survives: a name that does not resolve, a key
    the hub has not registered, and a hub whose own ``git`` cannot run all end
    in the same exit code.
    """


def _kill_command_tree(process: subprocess.Popen[str]) -> None:
    """Kill a remote Git command together with the ssh child it spawned.

    Killing only ``git`` would orphan an ssh still blocked in name
    resolution, and that orphan keeps the hang alive invisibly.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(OSError):
        process.kill()
    process.wait()


def _run_remote_git(
    repository: Repo,
    *arguments: str,
    timeout: float = REMOTE_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run one Git command that reaches the hub, bounded by a wall clock.

    GitPython runs Git without a timeout (its ``kill_after_timeout`` is
    POSIX-only), and ssh options cannot bound the hang either -- see
    ``REMOTE_COMMAND_TIMEOUT_SECONDS``. Local Git commands stay on GitPython;
    only the ones that reach the hub go through this boundary.

    Raises:
        HubTimeoutError: When the hub does not answer within the bound.
        HubCommandError: When Git itself reports the command failed.
    """
    argv: list[str] = [Git.GIT_PYTHON_GIT_EXECUTABLE or "git", *arguments]
    process = subprocess.Popen(
        argv,
        cwd=repository.working_dir,
        env={**os.environ, **repository.git.environment()},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_command_tree(process)
        raise HubTimeoutError(
            f"The hub did not answer 'git {arguments[0]}' within "
            f"{int(timeout)} seconds."
        ) from exc
    if process.returncode != 0:
        raise HubCommandError(
            stderr.strip()
            or f"'git {arguments[0]}' exited with code {process.returncode}."
        )
    return stdout


@dataclass(frozen=True)
class RefusedChange:
    """A changed path kept out of ``git add`` because Git cannot index it.

    Attributes:
        path (str): The path relative to ``.guildbotics/``, with no trailing
            slash. A directory Git reports as ``config/commands/`` is named
            ``config/commands`` here, which is also the name a later commit
            stages as a gitlink.
        reason (str): Why it stayed out, phrased to follow the path.
    """

    path: str
    reason: str


@dataclass(frozen=True)
class StagedChanges:
    """What staging one scan of the shared roots left in the index.

    Attributes:
        paths (tuple[str, ...]): Paths now staged.
        refused (tuple[RefusedChange, ...]): Embedded repositories with no
            commit, found before ``git add`` and kept out of it, so the cycle
            can hold them instead of failing.
    """

    paths: tuple[str, ...]
    refused: tuple[RefusedChange, ...]


@dataclass(frozen=True)
class SharedEntry:
    """One file as Git records it in a commit or in the index.

    Attributes:
        mode (str): The Git mode, for example ``100644`` or ``120000``.
        data (bytes): The content of a regular file, exactly as stored. Empty
            for anything else: a link's content is a path and a gitlink's is a
            commit this repository does not hold, and validation refuses both
            on their mode before content would matter.
    """

    mode: str
    data: bytes


@dataclass(frozen=True)
class RejectedChange:
    """One displaced commit this device still holds.

    Which files were not accepted is deliberately absent. It was decided when
    the commit was displaced -- against the hub's version at that moment -- and
    recorded then; the commit itself cannot answer it afterwards. A workspace
    that joined a hub has exactly one commit, so reading the files out of it
    would name everything the workspace had rather than the few that lost.

    Attributes:
        rejection_id (str): Names the ref holding it, and ties it to the
            activity event recorded when it was displaced.
        occurred_at (str): When the displaced commit was made.
    """

    rejection_id: str
    occurred_at: str


class LocalSyncRepository:
    """Git operations on ``<workspace>/.guildbotics``.

    Args:
        workspace_root (Path | None): The workspace, or None to use the selected one.
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root
        self.path = self._expected_path()

    @property
    def workspace_root(self) -> Path:
        """The resolved workspace this repository belongs to.

        Callers that need to reach other parts of the same workspace use this
        rather than resolving the selected workspace again, which could name a
        different one while a workspace is being switched.
        """
        return self.path.parent

    def verify_boundary(self) -> None:
        """Confirm this repository is still the workspace's own synchronized copy.

        Raises:
            SyncRepositoryError: When the selected workspace no longer resolves
                to this path, or when the path sits inside ``local/clones/``.
        """
        expected = self._expected_path()
        if expected != self.path:
            raise SyncRepositoryError(
                f"The selected workspace moved from {self.path} to {expected}."
            )
        for parent in self.path.parents:
            if parent.name == "clones" and parent.parent.name == "local":
                raise SyncRepositoryError(
                    f"{self.path} is inside a member working clone directory."
                )

    @property
    def initialized(self) -> bool:
        """True when ``.guildbotics`` is already a Git repository."""
        return (self.path / ".git").exists()

    @property
    def holds_shared_content(self) -> bool:
        """True when this workspace already has content of its own to keep.

        ``local/`` is deliberately not part of the answer. It is this device's
        own scratch -- opening a folder in the Desktop writes diagnostics there
        before the user has done anything with it -- so counting it would make
        every folder that was merely looked at indistinguishable from a
        workspace that must not be copied over.
        """
        return self.initialized or any(
            (self.path / root).exists() for root in SHARED_ROOTS
        )

    def initialize(self) -> None:
        """Make ``.guildbotics`` a repository holding the current shared state.

        Idempotent: an already initialized repository only has its ignore rules
        and commit identity refreshed.
        """
        self.verify_boundary()
        self.path.mkdir(parents=True, exist_ok=True)
        if not self.initialized:
            Repo.init(self.path, initial_branch=SYNC_BRANCH)
        repository = self._repo()
        # Replaced rather than written through, so a link put in its place
        # never carries this write to wherever it points.
        atomic_write_text(self.path / ".gitignore", GITIGNORE_CONTENT)
        # Every file is carried as the bytes validated: no line-ending, filter,
        # or encoding conversion on add or checkout, whatever a global
        # ``core.autocrlf`` or an untracked ``.gitattributes`` asks for. This
        # file outranks every other source of attributes.
        atomic_write_text(
            Path(repository.git_dir) / "info" / "attributes", GIT_ATTRIBUTES
        )
        # Commits are machine-generated bookkeeping, so they carry a fixed
        # identity instead of the member identity used for the user's own work.
        repository.git.config("user.name", "GuildBotics")
        repository.git.config("user.email", "sync@guildbotics.invalid")
        repository.git.config("commit.gpgsign", "false")
        # Without it, Git for Windows warns about a path over 260 characters
        # and leaves it out of status and add while still exiting 0.
        repository.git.config("core.longpaths", "true")

    def clone(self, url: str, accept: Callable[[str], None]) -> None:
        """Fill this repository with a copy of a hub's shared content.

        This is how a machine takes a workspace it has never held before. A
        workspace that already has content of its own joins instead, so that
        its files are committed and kept rather than overwritten by the copy.

        The destination is fetched into rather than cloned into, because
        ``.guildbotics`` is usually already there: selecting a folder in the
        Desktop opens it as a workspace, and this device's diagnostics land
        under ``local/`` before the copy is asked for. ``git clone`` refuses a
        directory that is not empty, which made the intended way of reaching
        this the one way that could not work.

        Args:
            url (str): The hub repository to copy.
            accept (Callable[[str], None]): Checks the fetched revision before
                anything of it is written to the working tree, raising to
                refuse it. A checkout writes links and anything else the hub
                holds exactly as recorded, so this is the one point where
                refusing still leaves nothing behind.

        Raises:
            SyncRepositoryError: When this workspace already holds content of
                its own.
        """
        self.verify_boundary()
        if self.holds_shared_content:
            raise SyncRepositoryError(
                f"{self.path} already holds a workspace. Enable synchronization "
                "on it instead of taking a second copy."
            )
        self.initialize()
        try:
            repository = self._repo()
            repository.create_remote(SYNC_REMOTE, url)
            _run_remote_git(repository, "fetch", SYNC_REMOTE, SYNC_BRANCH)
            accept(f"{SYNC_REMOTE}/{SYNC_BRANCH}")
            repository.git.checkout("-B", SYNC_BRANCH, f"{SYNC_REMOTE}/{SYNC_BRANCH}")
        except Exception:
            # A copy that could not be taken leaves nothing behind. What it
            # made is a repository carrying the hub as its remote, and the next
            # activation reads that as a connected workspace: it starts a
            # queue, which mints an identity into ``state/`` -- so the folder
            # the user was copying into ends up holding a workspace of its own,
            # and no second attempt can copy into it any more.
            self._discard()
            raise

    def _discard(self) -> None:
        """Undo :meth:`initialize`, leaving the folder as the copy found it."""
        shutil.rmtree(self.path / ".git", onexc=_remove_read_only)
        (self.path / ".gitignore").unlink(missing_ok=True)

    def stage_changes(self) -> StagedChanges:
        """Stage every shared change and report what Git refused to stage.

        This is what recovers a change whose save notification was lost, and
        what picks up an edit made directly with an external editor.

        Whole shared roots are staged rather than the paths a status lists.
        Git refuses to stage a path beneath a link by name, and replacing a
        tracked directory with a link leaves exactly such paths behind as
        deletions; staging the root records the link and those deletions
        together. The answer is read back from the index, so it names what is
        staged rather than what a status saw a moment earlier.

        Every path is found before ``git add`` runs, never guessed from what
        it left behind. An embedded repository with no commit cannot be
        indexed, and adding it fails the whole root. Status names an embedded
        repository, and only that, as a directory (``config/commands/``), so
        the ones with no commit are excluded from the add and returned as
        refused. One with a commit is added as a gitlink, which the mode check
        holds. Any other failure of the add fails the cycle: an unreadable
        file or a locked index is not a change to hold.

        Raises:
            SyncRepositoryError: When a directory under a shared root cannot be
                listed.
            GitCommandError: When ``git add`` fails.
        """
        _refuse_unlistable_directories(self.path)
        repository = self._repo()
        listed = _changed_paths(repository)
        # A root nothing lists may not exist, and Git refuses to add it then.
        roots = sorted({path.split("/", 1)[0] for path in listed} & set(SHARED_ROOTS))
        if not roots:
            return StagedChanges(paths=(), refused=())
        refused = tuple(
            RefusedChange(path=path, reason="is a Git repository with no commit")
            for path in (entry[:-1] for entry in listed if entry.endswith("/"))
            if (self.path / path / ".git").exists()
            and not _has_commit(self.path / path)
        )
        # Exclusion is pathspec magic, which literal pathspecs switch off; each
        # path is spelled ``literal`` instead, so a name is still never a pattern.
        repository.git.add(
            "--all",
            "--",
            *(f":(literal){root}" for root in roots),
            *(f":(exclude,literal){item.path}" for item in refused),
            env={"GIT_LITERAL_PATHSPECS": "0"},
        )
        staged = tuple(
            path
            for path in repository.git.diff(
                "--cached", "--name-only", "-z", "--no-renames", "--", *SHARED_ROOTS
            ).split("\0")
            if path
        )
        return StagedChanges(paths=staged, refused=refused)

    def read_entries(
        self, revision: str, paths: Sequence[str]
    ) -> dict[str, SharedEntry]:
        """Return the files ``revision`` records under ``paths``; absent ones are left out.

        Member avatars travel through here, so the bytes are returned exactly
        as stored rather than decoded or trimmed.
        """
        # ``<mode> SP <type> SP <object> TAB <path>``. Recursive, so a path that
        # names a directory there lists what it holds rather than the tree.
        listed = self._listed(
            paths,
            lambda batch: self._repo().git.ls_tree("-r", "-z", revision, "--", *batch),
        )
        return {
            path: self._entry(fields[0], fields[2]) for path, fields in listed.items()
        }

    def read_staged(self, paths: Sequence[str]) -> dict[str, SharedEntry]:
        """Return what is staged for ``paths``; one staged as gone is left out."""
        # ``<mode> SP <object> SP <stage> TAB <path>``
        listed = self._listed(
            paths, lambda batch: self._repo().git.ls_files("-s", "-z", "--", *batch)
        )
        return {
            path: self._entry(fields[0], fields[1]) for path, fields in listed.items()
        }

    def _listed(
        self, paths: Sequence[str], listing: Callable[[Sequence[str]], str]
    ) -> dict[str, list[str]]:
        """Map each of ``paths`` Git listed to the fields listed for it.

        A path naming a directory lists what is under it, so only an entry
        whose own path was asked for answers for it.
        """
        wanted = set(paths)
        found: dict[str, list[str]] = {}
        for batch in _batched(paths):
            for record in listing(batch).split("\0"):
                fields, _, path = record.partition("\t")
                if path in wanted:
                    found[path] = fields.split(" ")
        return found

    def _entry(self, mode: str, oid: str) -> SharedEntry:
        """Read the listed object, so the bytes are the entry's by construction."""
        if mode not in REGULAR_FILE_MODES:
            return SharedEntry(mode=mode, data=b"")
        content = self._repo().git.cat_file(
            "blob", oid, stdout_as_string=False, strip_newline_in_stdout=False
        )
        return SharedEntry(mode=mode, data=bytes(content))

    def unstage(self, paths: Sequence[str]) -> None:
        """Take ``paths`` back out of the index, leaving the working tree alone.

        Used for a staged file that turned out not to be shareable: the index
        goes back to what the last commit holds, and the content the user has
        to fix stays on disk exactly as they left it.
        """
        head = self.head()
        for batch in _batched(paths):
            if head is None:
                self._repo().git.rm("--cached", "--force", "--", *batch)
            else:
                self._repo().git.reset("--quiet", head, "--", *batch)

    def commit(self, message: str) -> str | None:
        """Commit whatever is staged, returning the new commit, or None if empty."""
        repository = self._repo()
        if not repository.git.diff("--cached", "--name-only"):
            return None
        repository.git.commit("--no-verify", "-m", message)
        return self.head()

    def head(self) -> str | None:
        """Return the commit the sync branch is on, or None before the first one."""
        try:
            return self._repo().git.rev_parse("HEAD")
        except GitCommandError:
            return None

    def remote_head(self) -> str | None:
        """Return the last fetched hub commit, or None when the hub has none."""
        try:
            return self._repo().git.rev_parse(f"{SYNC_REMOTE}/{SYNC_BRANCH}")
        except GitCommandError:
            return None

    def ahead_behind(self, local: str, remote: str) -> tuple[int, int]:
        """Return how many commits each side has that the other does not."""
        output = self._repo().git.rev_list(
            "--left-right", "--count", f"{local}...{remote}"
        )
        ahead, behind = output.split()
        return int(ahead), int(behind)

    def merge_base(self, left: str, right: str) -> str | None:
        """Return the commit both sides share, or None for unrelated histories."""
        try:
            return self._repo().git.merge_base(left, right)
        except GitCommandError:
            return None

    def changed_paths(self, base: str, head: str) -> dict[str, str]:
        """Map each path changed between two commits to its status letter.

        ``A`` for added, ``M`` for modified, ``D`` for deleted. Renames are not
        detected, so a move reads as one deletion and one addition, which is
        what concurrent-update comparison needs. A change of type -- a file
        that became a link -- is a modification like any other, so it is
        validated and compared rather than falling between the three.
        """
        output = self._repo().git.diff(
            "--name-status", "-z", "--no-renames", base, head
        )
        fields = [field for field in output.split("\0") if field]
        return {
            path: "M" if status[:1] == "T" else status[:1]
            for status, path in zip(fields[::2], fields[1::2], strict=True)
        }

    def has_remote(self) -> bool:
        """True when this workspace has been connected to a hub."""
        return SYNC_REMOTE in {remote.name for remote in self._repo().remotes}

    def remote_url(self) -> str | None:
        """Return the hub URL, or None when synchronization is not enabled."""
        if not self.has_remote():
            return None
        return self._repo().remote(SYNC_REMOTE).url

    def set_remote(self, url: str) -> None:
        """Point the repository at ``url``, adding the remote on first use."""
        repository = self._repo()
        if self.has_remote():
            repository.remote(SYNC_REMOTE).set_url(url)
        else:
            repository.create_remote(SYNC_REMOTE, url)

    def clear_remote(self) -> None:
        """Forget the hub, returning the workspace to not being synchronized.

        A connection attempt that fails partway must not leave a remote behind:
        the next start would see a workspace that looks connected and would run
        a queue against a hub that was never accepted.
        """
        if self.has_remote():
            repository = self._repo()
            repository.delete_remote(repository.remote(SYNC_REMOTE))

    def fetch(self) -> None:
        """Update the local view of the hub.

        The remote's own refspec is used rather than naming the branch, so a
        hub that has no commits yet is an empty answer rather than an error.

        Pruning is what makes "the hub has nothing" reachable at all. A hub
        rebuilt at the address the old one used answers an unpruned fetch with
        silence, and the remote-tracking ref left over from the hub that is gone
        keeps describing content the new one has never seen -- so this device
        concludes it is in sync and sends the rebuilt hub nothing.
        """
        _run_remote_git(self._repo(), "fetch", SYNC_REMOTE, "--prune")

    def fetch_preview(self, url: str) -> str | None:
        """Read a hub's content without connecting this workspace to it.

        Showing the user what joining would do must not leave the workspace
        looking joined, so the URL is used directly and no remote is recorded.

        The hub is asked what it has before anything is fetched. A hub that is
        empty and a hub that cannot be reached would otherwise both look like
        "nothing to fetch", and the user would be shown an empty hub they are
        about to register with when the real answer is that their key is not
        registered yet.

        Returns:
            str | None: The hub's commit, or None when the hub is still empty.

        Raises:
            HubCommandError: When the hub cannot be reached.
        """
        listing = _run_remote_git(
            self._repo(), "ls-remote", url, f"refs/heads/{SYNC_BRANCH}"
        )
        if not listing.strip():
            return None
        _run_remote_git(self._repo(), "fetch", url, f"+{SYNC_BRANCH}:{PREVIEW_REF}")
        return self._repo().git.rev_parse(PREVIEW_REF)

    def forget_preview(self) -> None:
        """Drop the ref a preview fetched, along with its claim on the objects.

        A hub the user decided not to join has no business keeping content in
        this repository.
        """
        with suppress(GitCommandError):
            self._repo().git.update_ref("-d", PREVIEW_REF)

    def push(self) -> None:
        """Send the sync branch to the hub, which accepts fast-forwards only."""
        _run_remote_git(
            self._repo(), "push", SYNC_REMOTE, f"{SYNC_BRANCH}:{SYNC_BRANCH}"
        )

    def move_to(self, commit: str) -> None:
        """Point the branch and index at ``commit``, leaving the working tree.

        The working tree is deliberately untouched so files held back by
        validation keep the content the user still has to fix.
        """
        self._repo().git.reset("--mixed", commit)

    def restore_from_index(self, paths: Sequence[str]) -> None:
        """Overwrite the working tree with the indexed content of ``paths``.

        Paths the index does not carry are removed from disk, which is how a
        deletion accepted by the hub is applied here.
        """
        indexed = self._indexed_paths()
        present = [path for path in paths if path in indexed]
        for batch in _batched(present):
            self._repo().git.checkout("--", *batch)
        for path in paths:
            if path not in indexed:
                (self.path / path).unlink(missing_ok=True)

    def save_rejected(self, rejection_id: str, commit: str) -> str:
        """Keep ``commit`` under a rejected ref so its content stays recoverable.

        The ref is never pushed: recovery happens on the device that made the
        change, from that device's own repository.
        """
        ref = f"{REJECTED_REF_PREFIX}/{rejection_id}"
        self._repo().git.update_ref(ref, commit)
        return ref

    def list_rejected(self) -> tuple[RejectedChange, ...]:
        """Return the displaced commits this device is still holding.

        Rejected refs are never pushed, so this is the whole of what can be
        recovered here -- and the whole of what the user can discard once they
        are done with it. Nothing is read out of the commits but the names of
        the files they touched.
        """
        listed = self._repo().git.for_each_ref(
            "--format=%(refname)%09%(committerdate:iso-strict)", REJECTED_REF_PREFIX
        )
        rejected: list[RejectedChange] = []
        for line in listed.splitlines():
            refname, _, occurred_at = line.partition("\t")
            if not refname:
                continue
            rejected.append(
                RejectedChange(
                    rejection_id=refname.rsplit("/", 1)[-1],
                    occurred_at=occurred_at,
                )
            )
        return tuple(rejected)

    def discard_rejected(self, rejection_id: str) -> bool:
        """Drop one rejected ref because the user said to.

        Nothing deletes these on its own: the ref is the only copy of the
        content it holds, on the only device holding it. Discarding is also the
        one thing the screen can offer without reading that content -- looking
        at it and exporting it stay a manual procedure on this device -- which
        is why it is the operation that exists here.

        Returns:
            bool: False when no such rejection is held, so a stale screen
                asking twice is an answer rather than an error.
        """
        try:
            uuid.UUID(rejection_id)
        except ValueError as exc:
            raise SyncRepositoryError(
                f"{rejection_id!r} does not name a rejected change."
            ) from exc
        ref = f"{REJECTED_REF_PREFIX}/{rejection_id}"
        try:
            self._repo().git.show_ref("--verify", "--quiet", ref)
        except GitCommandError:
            return False
        self._repo().git.update_ref("-d", ref)
        return True

    def rejected_id_for(self, commit: str) -> str | None:
        """Return the rejection identifier already stashing ``commit``, if any.

        Stopping between stashing a commit and adopting the hub's content
        leaves the same rejection to be handled again on the next run. Finding
        the existing ref keeps that restart from stashing a second copy and
        recording a second activity event for one rejection.
        """
        output = self._repo().git.for_each_ref(
            "--format=%(objectname) %(refname)", REJECTED_REF_PREFIX
        )
        for line in output.splitlines():
            objectname, _, refname = line.partition(" ")
            if objectname == commit:
                return refname.rsplit("/", 1)[-1]
        return None

    def _indexed_paths(self) -> set[str]:
        """Read the whole index once; a first clone restores thousands of files."""
        output = self._repo().git.ls_files("-z")
        return {path for path in output.split("\0") if path}

    def _expected_path(self) -> Path:
        return get_workspace_root(self._workspace_root) / ".guildbotics"

    def _repo(self) -> Repo:
        try:
            repository = Repo(self.path)
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise SyncRepositoryError(
                f"{self.path} is not a synchronization repository."
            ) from exc
        if Path(repository.working_tree_dir or "") != self.path:
            raise SyncRepositoryError(
                f"{self.path} belongs to the repository at "
                f"{repository.working_tree_dir}."
            )
        _configure_git(repository)
        return repository


def _remove_read_only(
    function: Callable[[str], object], path: str, _: BaseException
) -> None:
    """Retry a removal after making the entry writable, giving up quietly.

    Git writes its objects read-only, and Windows refuses to delete a read-only
    file, so a fetched copy would otherwise survive being discarded.
    """
    with suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
        function(path)


def _batched(paths: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(paths), _PATH_BATCH):
        yield paths[start : start + _PATH_BATCH]


def _changed_paths(repository: Repo) -> list[str]:
    """Return the paths a status of the shared roots lists right now.

    Ordinary directories are expanded to their files, so an entry that keeps
    its trailing slash (``config/commands/``) is an embedded repository.
    """
    status = repository.git.status(
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        "--",
        *SHARED_ROOTS,
    )
    return [entry[_STATUS_PREFIX:] for entry in status.split("\0") if entry]


def _has_commit(path: Path) -> bool:
    """True when ``path`` is a repository whose HEAD names a commit.

    ``-C`` rather than a working directory: GitPython runs a command whose
    working directory is gone in its own, and would answer for that one.
    """
    try:
        Git()(C=str(path)).rev_parse("--verify", "--quiet", "HEAD")
    except GitCommandError:
        return False
    return True


def _refuse_unlistable_directories(root: Path) -> None:
    """Fail when a directory under a shared root cannot be listed.

    Git only warns about such a directory and exits 0, and stages every
    tracked file inside it as deleted, which the hub then carries to every
    other device. A missing directory is not refused: it may vanish while the
    walk runs, and Windows reports a path too long to open the same way, which
    Git reaches through ``core.longpaths``. Hidden directories, an embedded
    repository's ``.git`` among them, are skipped: they are never shared.

    Raises:
        SyncRepositoryError: Naming the directory that cannot be listed.
    """

    def refuse(error: OSError) -> None:
        if isinstance(error, PermissionError):
            raise SyncRepositoryError(
                f"{error.filename} cannot be listed, so its files cannot be "
                "synchronized."
            ) from error

    for shared in SHARED_ROOTS:
        for _, directories, _ in os.walk(root / shared, onerror=refuse):
            directories[:] = [name for name in directories if not name.startswith(".")]


def _configure_git(repository: Repo) -> None:
    """Make every path a literal name and pin the hub commands' OpenSSH client.

    Paths here are names of files, never patterns: as a pattern, a held
    ``config/a[b].md`` also matches ``config/ab.md``, and unstaging or
    restoring the one would act on the other.

    On Windows, Git ships an ``ssh.exe`` of its own with its own
    ``known_hosts``, so a hub the user confirmed once would be an unknown host
    to ``git fetch``. A machine with no client at all is left alone: a hub
    reached through a filesystem path needs none.
    """
    repository.git.update_environment(GIT_LITERAL_PATHSPECS="1")
    with suppress(OpenSshNotFoundError):
        repository.git.update_environment(
            GIT_SSH_COMMAND=git_ssh_command(), GIT_TERMINAL_PROMPT="0"
        )
