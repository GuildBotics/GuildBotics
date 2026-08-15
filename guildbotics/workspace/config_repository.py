"""Config reads and writes guarded by a Git blob ID compare-and-set.

Config files are the shared state a person edits by hand, so two machines --
or one machine with a stale editor open -- can plausibly submit conflicting
edits. Every read therefore returns the content together with its Git blob ID,
and every write states the blob ID it expects to replace. A write whose
expectation no longer holds is refused rather than merged, and the caller
redisplays the current content.

The blob ID is the same value Git computes for that content, so shared files
need no version field of their own and the identifier survives synchronization
unchanged. The compare-and-set is a device-level mutex around read, compare,
and replace, not a lock held for the length of an edit: a distributed lock
would block local editing whenever the hub is unreachable, which the design
refuses to trade away.

This optimistic lock applies to config alone. Memory documents, conversation
state, and task runs keep their existing save APIs; when two devices really do
change the same file, the sync manager's first-committer-wins rule settles it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import get_workspace_config_dir, get_workspace_local_path
from guildbotics.utils.workspace_sync_port import write_shared_text
from guildbotics.workspace.validation import (
    SharedFileInvalidError,
    validate_shared_file,
)


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
        self._lock_path = get_workspace_local_path(
            "run", "config-write.lock", workspace_root=workspace_root
        )

    def read_config(self, relative_path: str) -> ConfigSnapshot | None:
        """Return the config file's content and revision, or None when absent.

        Args:
            relative_path (str): The path relative to the config directory,
                for example ``team/project.yml``.
        """
        return self._snapshot(relative_path)

    def write_config(
        self, relative_path: str, expected_blob_id: str | None, content: str
    ) -> ConfigSnapshot:
        """Replace a config file when it still holds ``expected_blob_id``.

        Args:
            relative_path (str): The path relative to the config directory.
            expected_blob_id (str | None): The revision the caller read, or
                None to require that the file does not exist yet.
            content (str): The complete new content.

        Returns:
            ConfigSnapshot: The newly written content and its revision.

        Raises:
            StaleConfigWriteError: When the stored revision differs, leaving
                the file untouched and announcing nothing.
            SharedFileInvalidError: When ``content`` is not valid for this
                file's kind, leaving the file untouched.
        """
        path = self._absolute_path(relative_path)
        shared_path = self._shared_path(relative_path)
        data = content.encode("utf-8")
        validate_shared_file(shared_path, data)
        with held_lock(self._lock_path):
            current = self._snapshot(relative_path)
            current_blob_id = current.blob_id if current is not None else None
            if current_blob_id != expected_blob_id:
                raise StaleConfigWriteError(shared_path, current)
            write_shared_text(path, content, workspace_root=self.workspace_root)
            # Describe the bytes this call committed rather than re-reading the
            # file. Reading after the lock would report the content of whoever
            # wrote next, which is not what this write applied.
            return ConfigSnapshot(
                relative_path=shared_path,
                path=path,
                blob_id=blob_id(data),
                content=content,
            )

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
