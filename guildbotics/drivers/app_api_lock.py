"""Machine-wide ownership of the backend, independent of service activity."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import IO, Literal

from guildbotics.utils.advisory_lock import (
    LockTimeoutError,
    held_lock,
    read_lock_data,
    write_lock_data,
)
from guildbotics.utils.fileio import get_machine_state_path

BackendOwner = Literal["cli", "desktop"]
OWNER_ENV = "GUILDBOTICS_APP_API_OWNER"
PORT_NOTICE_PREFIX = "GUILDBOTICS_APP_API_PORT="
CONFLICT_NOTICE_PREFIX = "GUILDBOTICS_APP_API_CONFLICT="


@dataclass
class BackendMetadata:
    owner: BackendOwner
    pid: int
    # Written by the API before stopping. A CLI parent can still distinguish
    # an intentional forced exit from a crash after the process is gone.
    stop_requested: bool = False


def _read(handle: IO[str]) -> BackendMetadata | None:
    try:
        data = json.loads(read_lock_data(handle))
        if (
            data["owner"] not in {"cli", "desktop"}
            or not isinstance(data["pid"], int)
            or data["pid"] <= 0
        ):
            return None
        return BackendMetadata(
            data["owner"], data["pid"], data.get("stop_requested") is True
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def read_backend_metadata() -> BackendMetadata | None:
    """Read the last holder's record; callers must match its PID or lock."""
    try:
        with get_machine_state_path("run", "app-api.lock").open(
            "r", encoding="utf-8"
        ) as handle:
            return _read(handle)
    except OSError:
        return None


def inspect_backend_lock() -> tuple[bool, BackendMetadata | None]:
    try:
        with held_lock(get_machine_state_path("run", "app-api.lock"), timeout=0):
            return False, None
    except LockTimeoutError:
        return True, read_backend_metadata()


class BackendAlreadyRunningError(RuntimeError):
    pass


class BackendLock:
    def __init__(self, handle: IO[str], owner: BackendOwner) -> None:
        self._handle = handle
        self._guard = threading.Lock()
        self.metadata = BackendMetadata(owner, os.getpid())
        self._write()

    def _write(self) -> None:
        write_lock_data(self._handle, json.dumps(asdict(self.metadata)) + "\n")

    def request_stop(self) -> None:
        with self._guard:
            self.metadata.stop_requested = True
            self._write()


@contextmanager
def hold_backend_lock() -> Iterator[BackendLock]:
    """Hold the lock before binding or touching any server records."""
    owner = os.environ.get(OWNER_ENV, "desktop")
    if owner not in {"cli", "desktop"}:
        raise ValueError(f"Invalid backend owner: {owner!r}")
    entered = False
    try:
        with held_lock(get_machine_state_path("run", "app-api.lock"), timeout=0) as f:
            entered = True
            yield BackendLock(f, "cli" if owner == "cli" else "desktop")
    except LockTimeoutError:
        if entered:
            raise
        metadata = read_backend_metadata()
        other = metadata.owner if metadata is not None else "unknown"
        print(f"{CONFLICT_NOTICE_PREFIX}{other}", flush=True)
        raise BackendAlreadyRunningError(other) from None
