"""File-based control channel for the machine-wide background service."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Literal
from uuid import uuid4

from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking,
    open_lock_file,
    unlock_file,
)
from guildbotics.utils.fileio import get_machine_state_path

StopStage = Literal["graceful", "cancel"]
_STAGE_RANK: dict[StopStage, int] = {"graceful": 0, "cancel": 1}
_LOCK_RETRY_SECONDS = 0.01
_LOCK_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class StopRequest:
    service_instance_id: str
    stage: StopStage

    @classmethod
    def from_dict(cls, value: object) -> StopRequest | None:
        if not isinstance(value, dict):
            return None
        instance_id = value.get("service_instance_id")
        stage = value.get("stage")
        if not isinstance(instance_id, str) or not instance_id:
            return None
        if stage not in _STAGE_RANK:
            return None
        return cls(service_instance_id=instance_id, stage=stage)


def stop_request_path() -> Path:
    return get_machine_state_path("run", "stop-request.json")


def read_stop_request(path: Path | None = None) -> StopRequest | None:
    request_path = path or stop_request_path()
    try:
        return StopRequest.from_dict(
            json.loads(request_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


@contextmanager
def _request_write_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as lock_file:
        _acquire_request_write_lock(lock_file)
        try:
            yield
        finally:
            unlock_file(lock_file)


def _acquire_request_write_lock(lock_file: IO[str]) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_file_nonblocking(lock_file)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Timed out acquiring the service-control lock."
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)


def write_stop_request(
    service_instance_id: str,
    stage: StopStage,
    path: Path | None = None,
) -> StopRequest:
    """Atomically publish a monotonic stop request for one service instance."""
    request_path = path or stop_request_path()
    request_path.parent.mkdir(parents=True, exist_ok=True)
    with _request_write_lock(request_path):
        current = read_stop_request(request_path)
        if (
            current is not None
            and current.service_instance_id == service_instance_id
            and _STAGE_RANK[current.stage] >= _STAGE_RANK[stage]
        ):
            return current

        request = StopRequest(service_instance_id=service_instance_id, stage=stage)
        temporary = request_path.with_name(
            f".{request_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(asdict(request), stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, request_path)
        finally:
            temporary.unlink(missing_ok=True)
        return request


def clear_stop_request(path: Path | None = None) -> None:
    request_path = path or stop_request_path()
    request_path.unlink(missing_ok=True)


class ServiceControlWatcher:
    """Watch one service-control file and dispatch matching requests."""

    def __init__(
        self,
        service_instance_id: str,
        request_shutdown: Callable[..., None],
        *,
        path: Path | None = None,
        poll_seconds: float = 0.1,
    ) -> None:
        self._service_instance_id = service_instance_id
        self._request_shutdown = request_shutdown
        self._path = path or stop_request_path()
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="service-control-watcher",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._poll_seconds * 2))

    def _run(self) -> None:
        last_stage: StopStage | None = None
        while not self._stop.wait(self._poll_seconds):
            request = read_stop_request(self._path)
            if (
                request is None
                or request.service_instance_id != self._service_instance_id
                or request.stage == last_stage
            ):
                continue
            last_stage = request.stage
            self._request_shutdown(cancel=request.stage == "cancel")
