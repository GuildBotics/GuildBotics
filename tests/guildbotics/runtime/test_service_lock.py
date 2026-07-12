from __future__ import annotations

import json

import pytest

from guildbotics.runtime import service_lock as service_lock_module
from guildbotics.runtime.service_lock import (
    ServiceLock,
    ServiceLockUnavailableError,
    inspect_service_lock,
)


def test_service_lock_is_exclusive_and_records_owner(tmp_path) -> None:
    path = tmp_path / "service.lock"
    first = ServiceLock(path)
    second = ServiceLock(path)

    metadata = first.acquire(owner="cli", workspace=tmp_path / "workspace")
    try:
        status = inspect_service_lock(path)
        assert status.locked is True
        assert status.metadata == metadata
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "owner": "cli",
            "pid": metadata.pid,
            "started_at": metadata.started_at,
            "workspace": str((tmp_path / "workspace").resolve()),
        }

        with pytest.raises(ServiceLockUnavailableError) as caught:
            second.acquire(owner="desktop", workspace=tmp_path / "other")
        assert caught.value.metadata == metadata
    finally:
        first.release()


def test_service_lock_release_keeps_file_but_makes_it_available(tmp_path) -> None:
    path = tmp_path / "service.lock"
    service_lock = ServiceLock(path)
    service_lock.acquire(owner="desktop", workspace=tmp_path)

    service_lock.release()

    assert path.exists()
    assert inspect_service_lock(path).locked is False


def test_service_lock_can_be_reacquired_by_another_owner(tmp_path) -> None:
    path = tmp_path / "service.lock"
    first = ServiceLock(path)
    first.acquire(owner="cli", workspace=tmp_path / "first")
    first.release()

    second = ServiceLock(path)
    metadata = second.acquire(owner="desktop", workspace=tmp_path / "second")
    try:
        assert metadata.owner == "desktop"
        assert inspect_service_lock(path).metadata == metadata
    finally:
        second.release()


def test_service_lock_retries_one_transient_conflict(monkeypatch, tmp_path) -> None:
    path = tmp_path / "service.lock"
    real_flock = service_lock_module.fcntl.flock
    attempts = 0

    def flaky_flock(file_descriptor: int, operation: int) -> None:
        nonlocal attempts
        if operation & service_lock_module.fcntl.LOCK_NB and attempts == 0:
            attempts += 1
            raise BlockingIOError
        real_flock(file_descriptor, operation)

    monkeypatch.setattr(service_lock_module.fcntl, "flock", flaky_flock)
    monkeypatch.setattr(service_lock_module.time, "sleep", lambda _seconds: None)
    service_lock = ServiceLock(path)

    metadata = service_lock.acquire(owner="cli", workspace=tmp_path)
    try:
        assert metadata.owner == "cli"
        assert attempts == 1
    finally:
        service_lock.release()
