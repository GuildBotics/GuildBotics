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

from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from guildbotics.utils.fileio import get_workspace_root
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
#: file of secrets must not reach the hub even if one is written by hand.
GITIGNORE_CONTENT = "local/\n.env\n"
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
        """Create this repository as a copy of a hub's shared content.

        This is how a machine takes a workspace it has never held before. A
        workspace that already has content joins instead, so that its own files
        are committed and kept rather than overwritten by the copy.

        Raises:
            SyncRepositoryError: When this workspace already has a repository.
        """
        self.verify_boundary()
        if self.initialized:
            raise SyncRepositoryError(
                f"{self.path} already exists. Enable synchronization on it "
                "instead of taking a second copy."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        environment = {"GIT_TERMINAL_PROMPT": "0"}
        with suppress(OpenSshNotFoundError):
            environment["GIT_SSH_COMMAND"] = git_ssh_command()
        Repo.clone_from(
            url, self.path, branch=SYNC_BRANCH, origin=SYNC_REMOTE, env=environment
        )

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

    def fetch(self) -> None:
        """Update the local view of the hub.

        The remote's own refspec is used rather than naming the branch, so a
        hub that has no commits yet is an empty answer rather than an error.
        """
        self._repo().git.fetch(SYNC_REMOTE)

    def fetch_preview(self, url: str) -> str | None:
        """Read a hub's content without connecting this workspace to it.

        Showing the user what joining would do must not leave the workspace
        looking joined, so the URL is used directly and no remote is recorded.

        Returns:
            str | None: The hub's commit, or None when the hub is still empty.
        """
        try:
            self._repo().git.fetch(url, f"+{SYNC_BRANCH}:{PREVIEW_REF}")
        except GitCommandError:
            # An empty hub has no branch to fetch. A hub that cannot be reached
            # at all fails later, when the workspace is actually connected.
            return None
        return self._repo().git.rev_parse(PREVIEW_REF)

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
