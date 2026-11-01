"""Machine-wide ownership of the backend, independent of service activity."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import IO

from guildbotics.utils.advisory_lock import (
    LockTimeoutError,
    held_lock,
    read_lock_data,
    write_lock_data,
)
from guildbotics.utils.fileio import get_machine_state_path

#: Set by ``guildbotics start``: the server starts its services once it
#: listens, with the settings saved on the Service screen.
START_SERVICES_ENV = "GUILDBOTICS_START_SERVICES"
#: The process that launched the server; the server exits once it is gone.
PARENT_PID_ENV = "GUILDBOTICS_APP_API_PARENT_PID"


@dataclass
class BackendMetadata:
    pid: int


def _read(handle: IO[str]) -> BackendMetadata | None:
    try:
        data = json.loads(read_lock_data(handle))
        if not isinstance(data["pid"], int) or data["pid"] <= 0:
            return None
        return BackendMetadata(data["pid"])
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


@contextmanager
def hold_backend_lock() -> Iterator[None]:
    """Hold the lock before binding or touching any server records."""
    entered = False
    try:
        with held_lock(get_machine_state_path("run", "app-api.lock"), timeout=0) as f:
            entered = True
            write_lock_data(f, json.dumps(asdict(BackendMetadata(os.getpid()))) + "\n")
            yield
    except LockTimeoutError:
        if entered:
            raise
        raise BackendAlreadyRunningError from None
