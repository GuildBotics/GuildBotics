"""Cross-process person execution lease and verified nested delegation."""

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

from guildbotics.runtime.advisory_lock import lock_file_nonblocking as _lock_nonblocking
from guildbotics.runtime.advisory_lock import open_lock_file as _open_lock_file
from guildbotics.runtime.advisory_lock import unlock_file as _unlock
from guildbotics.utils.fileio import get_workspace_data_root
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.safe_path import safe_path_component

LEASE_ID_ENV = "GUILDBOTICS_EXECUTION_LEASE_ID"
DELEGATION_ID_ENV = "GUILDBOTICS_EXECUTION_DELEGATION_ID"
LEASE_PERSON_ENV = "GUILDBOTICS_EXECUTION_PERSON_ID"
LEASE_RUN_ENV = "GUILDBOTICS_EXECUTION_RUN_ID"


@dataclass(frozen=True, slots=True)
class PersonLeaseMetadata:
    pid: int
    person_id: str
    lease_id: str
    delegation_id: str
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
                delegation_id=str(value["delegation_id"]),
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
    def __init__(self, person_id: str, data_root: Path | None = None) -> None:
        self.person_id = person_id
        self.path = (
            (data_root or get_workspace_data_root())
            / "run"
            / "person-leases"
            / (f"{safe_path_component(person_id)}.lock")
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
                    handle.close()
                    raise PersonLeaseUnavailableError(
                        _read_metadata(self.path)
                    ) from exc
            self._file = handle
            self._metadata = PersonLeaseMetadata(
                pid=os.getpid(),
                person_id=self.person_id,
                lease_id=uuid4().hex,
                delegation_id=uuid4().hex,
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
        handle.seek(0)
        handle.write(f"{payload}\n")
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def current_person_lease() -> PersonExecutionLease | None:
    return _current_lease.get()


def delegation_environment(run_id: str) -> dict[str, str]:
    lease = current_person_lease()
    if lease is None:
        return {}
    metadata = lease.bind_run_id(run_id)
    return {
        LEASE_ID_ENV: metadata.lease_id,
        DELEGATION_ID_ENV: metadata.delegation_id,
        LEASE_PERSON_ENV: metadata.person_id,
        LEASE_RUN_ENV: metadata.run_id,
    }


def validate_delegation(
    person_id: str,
    *,
    data_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> PersonLeaseMetadata | None:
    env = environ or os.environ
    lease_id = env.get(LEASE_ID_ENV, "")
    delegation_id = env.get(DELEGATION_ID_ENV, "")
    run_id = env.get(LEASE_RUN_ENV, "")
    if not lease_id or not delegation_id or not run_id:
        return None
    path = (
        (data_root or get_workspace_data_root())
        / "run"
        / "person-leases"
        / (f"{safe_path_component(person_id)}.lock")
    )
    handle = _open_lock_file(path)
    try:
        try:
            _lock_nonblocking(handle)
        except BlockingIOError:
            metadata = _read_metadata(path)
            if metadata is None:
                return None
            expected = (person_id, lease_id, delegation_id, run_id)
            actual = (
                metadata.person_id,
                metadata.lease_id,
                metadata.delegation_id,
                metadata.run_id,
            )
            return (
                metadata if actual == expected and _pid_exists(metadata.pid) else None
            )
        _unlock(handle)
        return None
    finally:
        handle.close()


def _pid_exists(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_metadata(path: Path) -> PersonLeaseMetadata | None:
    try:
        return PersonLeaseMetadata.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None
