"""Config reads and writes guarded by a Git blob ID compare-and-set.

Config files are the shared state a person edits by hand, so two machines --
or one machine with a stale editor open -- can plausibly submit conflicting
edits. Every read therefore returns the content together with its Git blob ID,
and every save states the blob IDs it expects to replace. A save whose
expectation no longer holds is refused rather than merged, and the caller
redisplays the current content.

The blob ID is the same value Git computes for that content, so shared files
need no version field of their own and the identifier survives synchronization
unchanged. The compare-and-set is a device-level mutex around read, compare,
and replace, not a lock held for the length of an edit: a distributed lock
would block local editing whenever the hub is unreachable, which the design
refuses to trade away.

One screen's save writes several config files, so the comparison covers all of
them and the writing happens inside the same lock: refusing a save that has
already applied half of itself would be the very outcome the check exists to
prevent. :meth:`ConfigRepository.guard` is therefore the write API -- it wraps
whatever the caller was already going to write, rather than asking every
config writer to hand its content over.

This optimistic lock applies to config alone. Memory documents, conversation
state, and task runs keep their existing save APIs; when two devices really do
change the same file, the sync manager's first-committer-wins rule settles it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from guildbotics.utils.fileio import get_workspace_config_dir
from guildbotics.workspace.shared_write_lock import shared_write_lock
from guildbotics.workspace.validation import SharedFileInvalidError

#: Ends a revision key that stands for a directory's set of files rather than
#: for one file's content. A config file's path never ends in a separator, so
#: the two forms cannot be confused for one another.
DIRECTORY_MARK = "/"


def blob_id(data: bytes) -> str:
    """Return the Git blob ID of ``data``.

    Git hashes ``blob <length>\\0`` followed by the content; this identifies a
    revision, and is never used as a security primitive.
    """
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


@dataclass(frozen=True)
class ConfigSnapshot:
    """One config file's content and the revision it was read at.

    Attributes:
        relative_path (str): The path relative to ``.guildbotics/``.
        path (Path): The absolute path on this device.
        blob_id (str): The revision to pass back as ``expected_blob_id``.
        content (str): The file's text.
    """

    relative_path: str
    path: Path
    blob_id: str
    content: str


class StaleConfigWriteError(RuntimeError):
    """Raised when a config write expected a revision that is no longer current.

    Attributes:
        snapshot (ConfigSnapshot | None): The content now stored, or None when
            the file has since been deleted.
    """

    def __init__(self, relative_path: str, snapshot: ConfigSnapshot | None) -> None:
        super().__init__(
            f"{relative_path} changed since it was read; the write was not applied."
        )
        self.relative_path = relative_path
        self.snapshot = snapshot


class ConfigRepository:
    """Read and write config files under compare-and-set.

    Args:
        workspace_root (Path | None): The workspace, or None to use the selected one.
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root
        self._config_dir = get_workspace_config_dir(workspace_root)

    def read_config(self, relative_path: str) -> ConfigSnapshot | None:
        """Return the config file's content and revision, or None when absent.

        Args:
            relative_path (str): The path relative to the config directory,
                for example ``team/project.yml``.
        """
        return self._snapshot(relative_path)

    def revisions(self, relative_paths: Iterable[str]) -> dict[str, str]:
        """Return the current revision of each named config file or directory.

        A file that does not exist is reported as an empty revision rather than
        left out, so its absence is itself what a later :meth:`guard` compares
        against: a file created meanwhile is a change like any other, and
        omitting it would let the save replace it unseen.

        Args:
            relative_paths (Iterable[str]): Paths relative to the config
                directory, for example ``team/project.yml``. A path ending in
                ``/`` names a directory and reads as its set of files.
        """
        return {path: self._revision(path) for path in relative_paths}

    def tree_revisions(self, relative_dir: str) -> dict[str, str]:
        """Return revisions describing a whole config directory.

        Screens that reconcile a directory -- writing some files, pruning
        others -- are stale if any file they read has changed since, and just
        as stale if the directory now holds a file they never saw: saving would
        prune it. One entry therefore stands for the set of paths itself, so a
        file another device added, and a directory that did not exist at all
        when the screen was read, are both changes there is something to
        compare against.

        Args:
            relative_dir (str): A directory relative to the config directory,
                for example ``intelligences``.
        """
        paths = self._tree_paths(relative_dir)
        return self.revisions([f"{relative_dir}{DIRECTORY_MARK}", *paths])

    @contextmanager
    def guard(self, expected: Mapping[str, str]) -> Iterator[None]:
        """Write config files only while they still hold the revisions read.

        The caller's own writes happen inside the ``with`` body and inside the
        lock, so a save that touches several files either applies completely or
        not at all.

        The lock is the workspace's shared-write lock rather than one of this
        module's own, so synchronization cannot check the hub's content out
        over these files between the comparison and the write.

        Args:
            expected (Mapping[str, str]): Config-relative path to the revision
                the caller read. A path left out is unconstrained; a path
                mapped to an empty string must still not exist; a path ending
                in ``/`` constrains that directory's set of files.

        Raises:
            StaleConfigWriteError: When one of the files changed since it was
                read. Nothing has been written when this is raised.
            LockTimeoutError: When synchronization holds the workspace's
                shared-write lock for longer than it waits.
        """
        with shared_write_lock(self.workspace_root):
            for relative_path, expected_blob_id in expected.items():
                if self._revision(relative_path) != expected_blob_id:
                    raise StaleConfigWriteError(
                        self._shared_path(relative_path),
                        self._snapshot(relative_path),
                    )
            yield

    def _revision(self, relative_path: str) -> str:
        """The revision of one config file, or of a directory's set of files."""
        if relative_path.endswith(DIRECTORY_MARK):
            return self._path_set_revision(
                self._tree_paths(relative_path[: -len(DIRECTORY_MARK)])
            )
        snapshot = self._snapshot(relative_path)
        return snapshot.blob_id if snapshot is not None else ""

    def _tree_paths(self, relative_dir: str) -> list[str]:
        """Config-relative paths of every file under ``relative_dir``, sorted."""
        root = self._absolute_path(relative_dir)
        if not root.is_dir():
            return []
        return [
            PurePosixPath(relative_dir)
            .joinpath(path.relative_to(root).as_posix())
            .as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]

    @staticmethod
    def _path_set_revision(paths: Sequence[str]) -> str:
        """A revision of which files exist, deliberately not of their content.

        Content is already covered file by file. A directory revision that
        moved with it as well would refuse a save whose own earlier step wrote
        one of those files -- a conflict with nobody.
        """
        return blob_id("".join(f"{path}\n" for path in paths).encode())

    def _absolute_path(self, relative_path: str) -> Path:
        """Resolve a config-relative path, refusing anything outside config/.

        Raises:
            SharedFileInvalidError: When the path is absolute, walks up with
                ``..``, or otherwise lands outside the config directory.
        """
        candidate = PurePosixPath(relative_path)
        if not relative_path.strip():
            raise SharedFileInvalidError(relative_path, "names no config file")
        if candidate.is_absolute() or PureWindowsPath(relative_path).is_absolute():
            raise SharedFileInvalidError(relative_path, "is an absolute path")
        if ".." in candidate.parts:
            raise SharedFileInvalidError(relative_path, "walks outside config/")
        path = self._config_dir / relative_path
        # Symlinks can leave the directory without a ``..`` in the path, so the
        # resolved location is what decides.
        if not path.resolve(strict=False).is_relative_to(
            self._config_dir.resolve(strict=False)
        ):
            raise SharedFileInvalidError(relative_path, "resolves outside config/")
        return path

    def _shared_path(self, relative_path: str) -> str:
        return (PurePosixPath("config") / PurePosixPath(relative_path)).as_posix()

    def _snapshot(self, relative_path: str) -> ConfigSnapshot | None:
        path = self._absolute_path(relative_path)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return ConfigSnapshot(
            relative_path=self._shared_path(relative_path),
            path=path,
            blob_id=blob_id(data),
            content=data.decode("utf-8"),
        )
