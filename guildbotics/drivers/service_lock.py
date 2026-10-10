from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Literal
from uuid import uuid4

from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking as _lock_file_nonblocking,
)
from guildbotics.utils.advisory_lock import open_lock_file as _open_lock_file
from guildbotics.utils.advisory_lock import read_lock_data as _read_lock_data
from guildbotics.utils.advisory_lock import unlock_file as _unlock_file
from guildbotics.utils.advisory_lock import write_lock_data as _write_lock_data
from guildbotics.utils.fileio import (
    get_machine_state_path,
    get_workspace_local_path,
    load_yaml_file,
    save_yaml_file,
)
from guildbotics.utils.keep_awake import KeepAwake
from guildbotics.utils.log_utils import get_logger

ServiceOwner = Literal["cli", "desktop"]
LOCK_RETRY_SECONDS = 0.01
#: The service's settings for this device, under the workspace's ``local/``:
#: whether the machine sleeps while the service waits is a property of the
#: machine, not of the workspace shared between machines.
SETTINGS_FILE = "service.yml"
#: How often the holding process reads the setting again while it holds the
#: lock. The setting is written by whoever the user switches it from (the
#: Desktop's API, which may run in another process than the service, or an
#: editor), so the holder follows the file rather than being told.
SETTING_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class ServiceLockMetadata:
    pid: int
    service_instance_id: str
    owner: ServiceOwner
    workspace: str
    started_at: str

    @classmethod
    def from_dict(cls, value: object) -> ServiceLockMetadata | None:
        if not isinstance(value, dict):
            return None
        try:
            pid = int(value["pid"])
            service_instance_id = str(value["service_instance_id"])
            owner = value["owner"]
            workspace = str(value["workspace"])
            started_at = str(value["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if pid <= 0 or not service_instance_id or owner not in {"cli", "desktop"}:
            return None
        return cls(
            pid=pid,
            service_instance_id=service_instance_id,
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


def service_keeps_awake(workspace: Path | None = None) -> bool:
    """Whether the service keeps this machine out of idle sleep while it runs.

    Off unless this device turned it on.
    """
    path = get_workspace_local_path(SETTINGS_FILE, workspace_root=workspace)
    if not path.is_file():
        return False
    data = load_yaml_file(path)
    return isinstance(data, dict) and data.get("keep_awake") is True


def set_service_keeps_awake(enabled: bool) -> None:
    """Record for this device whether the service keeps the machine awake."""
    path = get_workspace_local_path(SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(path, {"keep_awake": enabled})


class ServiceLock:
    """Own the machine-wide background service lock for one process.

    The span the lock is held is the span the service runs, so it is also the
    span the service keeps the machine awake when its workspace says so. The
    setting is read when the hold begins and again every
    :data:`SETTING_POLL_SECONDS` while it lasts.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_machine_state_path("run", "service.lock")
        self._guard = threading.Lock()
        self._file: IO[str] | None = None
        self._metadata: ServiceLockMetadata | None = None
        self._awake = KeepAwake()
        self._watch: tuple[threading.Thread, threading.Event] | None = None

    @property
    def locked(self) -> bool:
        with self._guard:
            return self._file is not None

    def acquire(
        self,
        *,
        owner: ServiceOwner,
        workspace: Path,
        before_publish: Callable[[], None] | None = None,
    ) -> ServiceLockMetadata:
        with self._guard:
            return self._acquire(
                owner=owner,
                workspace=workspace,
                before_publish=before_publish,
            )

    def _acquire(
        self,
        *,
        owner: ServiceOwner,
        workspace: Path,
        before_publish: Callable[[], None] | None,
    ) -> ServiceLockMetadata:
        if self._file is not None:
            if self._metadata is None:
                raise RuntimeError("A held service lock has no metadata.")
            return self._metadata

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = _open_lock_file(self.path)
        for attempt in range(2):
            try:
                _lock_file_nonblocking(lock_file)
                break
            except BlockingIOError as exc:
                if attempt == 0:
                    time.sleep(LOCK_RETRY_SECONDS)
                    continue
                metadata = _read_metadata(lock_file)
                lock_file.close()
                raise ServiceLockUnavailableError(metadata) from exc

        if before_publish is not None:
            try:
                before_publish()
            except Exception:
                _unlock_file(lock_file)
                lock_file.close()
                raise

        metadata = ServiceLockMetadata(
            pid=os.getpid(),
            service_instance_id=uuid4().hex,
            owner=owner,
            workspace=str(workspace.expanduser().resolve(strict=False)),
            started_at=datetime.now().astimezone().isoformat(),
        )
        try:
            payload = json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True)
            _write_lock_data(lock_file, f"{payload}\n")
            self._follow_keep_awake(metadata)
            watch = self._watch_keep_awake(metadata)
        except Exception:
            self._awake.stop()
            _unlock_file(lock_file)
            lock_file.close()
            raise

        self._file = lock_file
        self._metadata = metadata
        self._watch = watch
        return metadata

    def _follow_keep_awake(self, metadata: ServiceLockMetadata) -> None:
        if service_keeps_awake(Path(metadata.workspace)):
            self._awake.start()
        else:
            self._awake.stop()

    def _watch_keep_awake(
        self, metadata: ServiceLockMetadata
    ) -> tuple[threading.Thread, threading.Event]:
        stopped = threading.Event()
        thread = threading.Thread(
            target=self._keep_following,
            args=(metadata, stopped),
            name="guildbotics-service-keep-awake",
            daemon=True,
        )
        thread.start()
        return thread, stopped

    def _keep_following(
        self, metadata: ServiceLockMetadata, stopped: threading.Event
    ) -> None:
        path = get_workspace_local_path(
            SETTINGS_FILE, workspace_root=Path(metadata.workspace)
        )
        read: tuple[int, int, int] | None = None
        unreadable = False
        while not stopped.wait(SETTING_POLL_SECONDS):
            # Opened only when it changed: on Windows an open file cannot be
            # replaced, so reading it every time would fail the writer's save.
            seen = _signature(path)
            if read is not None and seen == read:
                continue
            with self._guard:
                # Set under the guard by release, so nothing is held after it.
                if stopped.is_set():
                    return
                try:
                    self._follow_keep_awake(metadata)
                except Exception as exc:
                    # Half-written by hand: keep what is held until it reads.
                    if not unreadable:
                        get_logger().warning(
                            "The service's keep-awake setting cannot be read; "
                            "keeping the machine as it is until it can: %s",
                            exc,
                        )
                    unreadable = True
                else:
                    read = seen
                    unreadable = False

    def release(self) -> None:
        with self._guard:
            lock_file = self._file
            watch = self._watch
            self._file = None
            self._metadata = None
            self._watch = None
            if lock_file is None:
                return
            if watch is not None:
                watch[1].set()
            self._awake.stop()
            try:
                _unlock_file(lock_file)
            finally:
                lock_file.close()
        if watch is not None:
            watch[0].join()


def inspect_service_lock(path: Path | None = None) -> ServiceLockStatus:
    """Return the active lock owner, ignoring stale file contents when unlocked."""
    lock_path = path or get_machine_state_path("run", "service.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _open_lock_file(lock_path)
    try:
        try:
            _lock_file_nonblocking(lock_file)
        except BlockingIOError:
            return ServiceLockStatus(locked=True, metadata=_read_metadata(lock_file))
        _unlock_file(lock_file)
        return ServiceLockStatus(locked=False)
    finally:
        lock_file.close()


def _signature(path: Path) -> tuple[int, int, int]:
    """What changes when the file is replaced or edited, read without opening
    it; a missing file is a signature of its own."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (-1, -1, -1)
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _read_metadata(handle: IO[str]) -> ServiceLockMetadata | None:
    try:
        return ServiceLockMetadata.from_dict(json.loads(_read_lock_data(handle)))
    except (OSError, ValueError):
        return None
