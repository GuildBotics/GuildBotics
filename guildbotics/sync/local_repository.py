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

import shutil
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from guildbotics.utils.fileio import ATOMIC_WRITE_SUFFIX, get_workspace_root
from guildbotics.utils.openssh import OpenSshNotFoundError, git_ssh_command
from guildbotics.utils.workspace_sync_port import SHARED_ROOTS

#: The only branch normal operation shares. Users are given no branch controls.
SYNC_BRANCH = "main"
#: The hub remote. A workspace without it is initialized but not yet connected.
SYNC_REMOTE = "origin"
#: Where a local commit goes when the hub already accepted a change to its paths.
REJECTED_REF_PREFIX = "refs/guildbotics/rejected"
#: Where a hub's content is read before the workspace is connected to it.
PREVIEW_REF = "refs/guildbotics/hub-preview"
#: ``local/`` is device-only; ``.env`` is refused a second time here because a
#: file of secrets must not reach the hub even if one is written by hand. The
#: third entry is an atomic write in progress: it is created beside its
#: destination, so for a shared file it lands inside this tree, and a cycle
#: that enumerates it either commits a half-written name or fails its own
#: ``git add`` when the rename beats it -- reported as a hub it could not
#: reach. It is never a file the user meant to share.
GITIGNORE_CONTENT = f"local/\n.env\n*{ATOMIC_WRITE_SUFFIX}\n"
#: Command lines stay bounded when a rescan finds thousands of changed files.
_PATH_BATCH = 200
#: ``git status --porcelain`` prefixes every entry with ``XY `` before the path.
_STATUS_PREFIX = 3


class SyncRepositoryError(RuntimeError):
    """Raised when the local synchronization repository cannot be operated."""


@dataclass(frozen=True)
class WorkingTreeChange:
    """One shared file that differs from the commit the repository is on.

    Attributes:
        path (str): The path relative to ``.guildbotics/``.
        deleted (bool): True when the file is gone from the working tree.
    """

    path: str
    deleted: bool


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
        (self.path / ".gitignore").write_text(GITIGNORE_CONTENT, encoding="utf-8")
        # Commits are machine-generated bookkeeping, so they carry a fixed
        # identity instead of the member identity used for the user's own work.
        repository.git.config("user.name", "GuildBotics")
        repository.git.config("user.email", "sync@guildbotics.invalid")
        repository.git.config("commit.gpgsign", "false")

    def clone(self, url: str) -> None:
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
            repository.git.fetch(SYNC_REMOTE, SYNC_BRANCH)
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
        shutil.rmtree(self.path / ".git", ignore_errors=True)
        (self.path / ".gitignore").unlink(missing_ok=True)

    def working_tree_changes(self) -> list[WorkingTreeChange]:
        """Return every shared file that differs from the current commit.

        This is what recovers a change whose save notification was lost, and
        what picks up an edit made directly with an external editor.
        """
        output = self._repo().git.status(
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            "--",
            *SHARED_ROOTS,
        )
        changes: list[WorkingTreeChange] = []
        for entry in output.split("\0"):
            # ``-z`` leaves the path itself verbatim after the status prefix.
            if len(entry) <= _STATUS_PREFIX:
                continue
            path = entry[_STATUS_PREFIX:]
            changes.append(
                WorkingTreeChange(path=path, deleted=not (self.path / path).exists())
            )
        return changes

    def read_working_tree(self, path: str) -> bytes:
        """Return the bytes currently on disk for a ``.guildbotics``-relative path."""
        return (self.path / path).read_bytes()

    def read_blob(self, revision: str, path: str) -> bytes | None:
        """Return a file's bytes at ``revision``, or None when it is absent there.

        Member avatars travel through here, so the bytes are returned exactly
        as stored rather than decoded or trimmed.
        """
        try:
            content = self._repo().git.cat_file(
                "blob",
                f"{revision}:{path}",
                stdout_as_string=False,
                strip_newline_in_stdout=False,
            )
        except GitCommandError:
            return None
        return bytes(content)

    def stage(self, paths: Sequence[str]) -> None:
        """Stage the given paths, recording deletions as deletions."""
        for batch in _batched(paths):
            self._repo().git.add("--", *batch)

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

    def read_staged(self, path: str) -> bytes | None:
        """Return the bytes staged for ``path``, or None when it is staged as gone."""
        try:
            content = self._repo().git.cat_file(
                "blob",
                f":0:{path}",
                stdout_as_string=False,
                strip_newline_in_stdout=False,
            )
        except GitCommandError:
            return None
        return bytes(content)

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
        what concurrent-update comparison needs.
        """
        output = self._repo().git.diff(
            "--name-status", "-z", "--no-renames", base, head
        )
        fields = [field for field in output.split("\0") if field]
        return {
            path: status[:1]
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
        self._repo().git.fetch(SYNC_REMOTE, "--prune")

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
            GitCommandError: When the hub cannot be reached.
        """
        listing = str(self._repo().git.ls_remote(url, f"refs/heads/{SYNC_BRANCH}"))
        if not listing.strip():
            return None
        self._repo().git.fetch(url, f"+{SYNC_BRANCH}:{PREVIEW_REF}")
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
        self._repo().git.push(SYNC_REMOTE, f"{SYNC_BRANCH}:{SYNC_BRANCH}")

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
        _apply_ssh_client(repository)
        return repository


def _batched(paths: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(paths), _PATH_BATCH):
        yield paths[start : start + _PATH_BATCH]


def _apply_ssh_client(repository: Repo) -> None:
    """Pin Git to the same OpenSSH client the hub commands use.

    On Windows, Git ships an ``ssh.exe`` of its own with its own
    ``known_hosts``, so a hub the user confirmed once would be an unknown host
    to ``git fetch``. A machine with no client at all is left alone: a hub
    reached through a filesystem path needs none.
    """
    with suppress(OpenSshNotFoundError):
        repository.git.update_environment(
            GIT_SSH_COMMAND=git_ssh_command(), GIT_TERMINAL_PROMPT="0"
        )
