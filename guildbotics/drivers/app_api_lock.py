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

#: Set by ``guildbotics server start --services``: the server starts its
#: services once it listens, with the settings saved on the Service screen.
START_SERVICES_ENV = "GUILDBOTICS_START_SERVICES"
#: How long the lifespan teardown may take. It has to cover every
#: wait the teardown can run into, each at its limit: a forced runtime stop
#: (event listener join, scheduler shutdown, scheduler join, work drain) and
#: the diagnostics maintenance join.
TEARDOWN_BUDGET_SECONDS = 60.0
#: How long a forced stop leaves the server before it kills it: the teardown's
#: budget and a margin for the waits before it (the services' forced stop, the
#: requests still open). ``ExitTimeOut`` in ``scripts/hub-server.sh`` matches.
KILL_AFTER_SECONDS = 90

#: The process that launched the server; the server exits once it is gone.
PARENT_PID_ENV = "GUILDBOTICS_APP_API_PARENT_PID"


@dataclass
class BackendMetadata:
    pid: int
    #: The port it listens on, so that what stops it reaches it whatever the
    #: environment of the one stopping it names.
    port: int


def _read(handle: IO[str]) -> BackendMetadata | None:
    try:
        data = json.loads(read_lock_data(handle))
        if any(
            not isinstance(data[key], int) or data[key] <= 0 for key in ("pid", "port")
        ):
            return None
        return BackendMetadata(data["pid"], data["port"])
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
def hold_backend_lock(port: int) -> Iterator[None]:
    """Hold the lock, naming the server's ``port``, before binding or touching
    any server records."""
    entered = False
    try:
        with held_lock(get_machine_state_path("run", "app-api.lock"), timeout=0) as f:
            entered = True
            metadata = BackendMetadata(os.getpid(), port)
            write_lock_data(f, json.dumps(asdict(metadata)) + "\n")
            yield
    except LockTimeoutError:
        if entered:
            raise
        raise BackendAlreadyRunningError from None
