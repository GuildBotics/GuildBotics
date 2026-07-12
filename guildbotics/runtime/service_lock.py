from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Literal

from guildbotics.utils.fileio import get_machine_state_path

ServiceOwner = Literal["cli", "desktop"]
LOCK_RETRY_SECONDS = 0.01


@dataclass(frozen=True)
class ServiceLockMetadata:
    pid: int
    owner: ServiceOwner
    workspace: str
    started_at: str

    @classmethod
    def from_dict(cls, value: object) -> ServiceLockMetadata | None:
        if not isinstance(value, dict):
            return None
        try:
            pid = int(value["pid"])
            owner = value["owner"]
            workspace = str(value["workspace"])
            started_at = str(value["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if pid <= 0 or owner not in {"cli", "desktop"}:
            return None
        return cls(
            pid=pid,
            owner=owner,
            workspace=workspace,
            started_at=started_at,
        )


@dataclass(frozen=True)
class ServiceLockStatus:
    locked: bool
    metadata: ServiceLockMetadata | None = None


class ServiceLockUnavailableError(RuntimeError):
    def __init__(self, metadata: ServiceLockMetadata | None) -> None:
        super().__init__("The background service lock is already held.")
        self.metadata = metadata


class ServiceLock:
    """Own the machine-wide background service lock for one process."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_machine_state_path("run", "service.lock")
        self._guard = threading.Lock()
        self._file: IO[str] | None = None
        self._metadata: ServiceLockMetadata | None = None

    @property
    def locked(self) -> bool:
        with self._guard:
            return self._file is not None

    def acquire(self, *, owner: ServiceOwner, workspace: Path) -> ServiceLockMetadata:
        with self._guard:
            return self._acquire(owner=owner, workspace=workspace)

    def _acquire(self, *, owner: ServiceOwner, workspace: Path) -> ServiceLockMetadata:
        if self._file is not None:
            if self._metadata is None:
                raise RuntimeError("A held service lock has no metadata.")
            return self._metadata

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        for attempt in range(2):
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if attempt == 0:
                    time.sleep(LOCK_RETRY_SECONDS)
                    continue
                lock_file.close()
                raise ServiceLockUnavailableError(_read_metadata(self.path)) from exc

        metadata = ServiceLockMetadata(
            pid=os.getpid(),
            owner=owner,
            workspace=str(workspace.expanduser().resolve(strict=False)),
            started_at=datetime.now().astimezone().isoformat(),
        )
        try:
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(asdict(metadata), lock_file, ensure_ascii=False, sort_keys=True)
            lock_file.write("\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except Exception:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise

        self._file = lock_file
        self._metadata = metadata
        return metadata

    def release(self) -> None:
        with self._guard:
            lock_file = self._file
            self._file = None
            self._metadata = None
            if lock_file is None:
                return
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def inspect_service_lock(path: Path | None = None) -> ServiceLockStatus:
    """Return the active lock owner, ignoring stale file contents when unlocked."""
    lock_path = path or get_machine_state_path("run", "service.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ServiceLockStatus(locked=True, metadata=_read_metadata(lock_path))
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return ServiceLockStatus(locked=False)
    finally:
        lock_file.close()


def _read_metadata(path: Path) -> ServiceLockMetadata | None:
    try:
        return ServiceLockMetadata.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None
