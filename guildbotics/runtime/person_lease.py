"""Cross-process person execution lease."""

from __future__ import annotations

import json
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from guildbotics.utils.advisory_lock import lock_file_nonblocking as _lock_nonblocking
from guildbotics.utils.advisory_lock import open_lock_file as _open_lock_file
from guildbotics.utils.advisory_lock import read_lock_data as _read_lock_data
from guildbotics.utils.advisory_lock import unlock_file as _unlock
from guildbotics.utils.advisory_lock import write_lock_data as _write_lock_data
from guildbotics.utils.fileio import get_workspace_local_path
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.safe_path import safe_path_component


@dataclass(frozen=True, slots=True)
class PersonLeaseMetadata:
    pid: int
    person_id: str
    lease_id: str
    source: str
    command: str
    work_id: str
    run_id: str
    started_at: str

    @classmethod
    def from_dict(cls, value: object) -> PersonLeaseMetadata | None:
        if not isinstance(value, dict):
            return None
        try:
            metadata = cls(
                pid=int(value["pid"]),
                person_id=str(value["person_id"]),
                lease_id=str(value["lease_id"]),
                source=str(value["source"]),
                command=str(value["command"]),
                work_id=str(value["work_id"]),
                run_id=str(value.get("run_id", "")),
                started_at=str(value["started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return metadata if metadata.pid > 0 and metadata.person_id else None


class PersonLeaseUnavailableError(RuntimeError):
    def __init__(self, metadata: PersonLeaseMetadata | None) -> None:
        self.metadata = metadata
        if metadata is None:
            message = t("cli.member.lease.unavailable")
        else:
            message = t(
                "cli.member.lease.unavailable_with_holder",
                pid=metadata.pid,
                source=metadata.source,
                command=metadata.command,
            )
        super().__init__(message)


_current_lease: ContextVar[PersonExecutionLease | None] = ContextVar(
    "guildbotics_person_execution_lease", default=None
)


class PersonExecutionLease:
    def __init__(self, person_id: str, workspace_root: Path | None = None) -> None:
        self.person_id = person_id
        self.path = get_workspace_local_path(
            "run",
            "person-leases",
            f"{safe_path_component(person_id)}.lock",
            workspace_root=workspace_root,
        )
        self._guard = threading.RLock()
        self._file: IO[str] | None = None
        self._metadata: PersonLeaseMetadata | None = None
        self._context_token: Any = None

    @property
    def metadata(self) -> PersonLeaseMetadata:
        if self._metadata is None:
            raise RuntimeError("Person execution lease is not held.")
        return self._metadata

    def acquire(
        self, *, source: str, command: str, work_id: str
    ) -> PersonLeaseMetadata:
        with self._guard:
            if self._file is not None:
                return self.metadata
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = _open_lock_file(self.path)
            for attempt in range(2):
                try:
                    _lock_nonblocking(handle)
                    break
                except BlockingIOError as exc:
                    if attempt == 0:
                        time.sleep(0.01)
                        continue
                    metadata = _read_metadata(handle)
                    handle.close()
                    raise PersonLeaseUnavailableError(metadata) from exc
            self._file = handle
            self._metadata = PersonLeaseMetadata(
                pid=os.getpid(),
                person_id=self.person_id,
                lease_id=uuid4().hex,
                source=source,
                command=command,
                work_id=work_id,
                run_id="",
                started_at=datetime.now().astimezone().isoformat(),
            )
            self._write_metadata()
            self._context_token = _current_lease.set(self)
            return self._metadata

    def bind_run_id(self, run_id: str) -> PersonLeaseMetadata:
        with self._guard:
            if not run_id or self.metadata.run_id == run_id:
                return self.metadata
            if self.metadata.run_id and self.metadata.run_id != run_id:
                raise RuntimeError(
                    "Execution lease is already bound to another run id."
                )
            self._metadata = replace(self.metadata, run_id=run_id)
            self._write_metadata()
            return self.metadata

    def unbind_run_id(self, run_id: str) -> PersonLeaseMetadata:
        """Release one completed delegation without weakening another binding."""
        with self._guard:
            if not run_id or self.metadata.run_id != run_id:
                return self.metadata
            self._metadata = replace(self.metadata, run_id="")
            self._write_metadata()
            return self.metadata

    def release(self) -> None:
        with self._guard:
            handle = self._file
            self._file = None
            self._metadata = None
            if self._context_token is not None:
                _current_lease.reset(self._context_token)
                self._context_token = None
            if handle is None:
                return
            try:
                _unlock(handle)
            finally:
                handle.close()

    def _write_metadata(self) -> None:
        handle = self._file
        if handle is None:
            raise RuntimeError("Person execution lease is not held.")
        payload = json.dumps(asdict(self.metadata), ensure_ascii=False, sort_keys=True)
        _write_lock_data(handle, f"{payload}\n")


def current_person_lease() -> PersonExecutionLease | None:
    return _current_lease.get()


def _read_metadata(handle: IO[str]) -> PersonLeaseMetadata | None:
    try:
        return PersonLeaseMetadata.from_dict(json.loads(_read_lock_data(handle)))
    except (OSError, ValueError):
        return None
