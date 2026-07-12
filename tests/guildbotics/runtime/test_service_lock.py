from __future__ import annotations

import errno
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
    real_lock = service_lock_module._lock_file_nonblocking
    attempts = 0

    def flaky_lock(lock_file) -> None:
        nonlocal attempts
        if attempts == 0:
            attempts += 1
            raise BlockingIOError
        real_lock(lock_file)

    monkeypatch.setattr(service_lock_module, "_lock_file_nonblocking", flaky_lock)
    monkeypatch.setattr(service_lock_module.time, "sleep", lambda _seconds: None)
    service_lock = ServiceLock(path)

    metadata = service_lock.acquire(owner="cli", workspace=tmp_path)
    try:
        assert metadata.owner == "cli"
        assert attempts == 1
    finally:
        service_lock.release()


def test_windows_lock_backend_uses_one_byte_range(monkeypatch, tmp_path) -> None:
    calls: list[tuple[int, int, int]] = []

    class FakeWindowsLocking:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(file_descriptor: int, mode: int, length: int) -> None:
            calls.append((file_descriptor, mode, length))

    monkeypatch.setattr(service_lock_module, "_WINDOWS", True)
    monkeypatch.setattr(service_lock_module, "_windows_locking", FakeWindowsLocking)
    path = tmp_path / "service.lock"

    with path.open("a+", encoding="utf-8") as lock_file:
        service_lock_module._lock_file_nonblocking(lock_file)
        service_lock_module._unlock_file(lock_file)

    assert [mode for _fd, mode, _length in calls] == [
        FakeWindowsLocking.LK_NBLCK,
        FakeWindowsLocking.LK_UNLCK,
    ]
    assert all(length == 1 for _fd, _mode, length in calls)
    assert path.stat().st_size == 1


def test_windows_lock_conflict_becomes_blocking_error(monkeypatch, tmp_path) -> None:
    class BusyWindowsLocking:
        LK_NBLCK = 1

        @staticmethod
        def locking(_file_descriptor: int, _mode: int, _length: int) -> None:
            raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(service_lock_module, "_WINDOWS", True)
    monkeypatch.setattr(service_lock_module, "_windows_locking", BusyWindowsLocking)

    with (tmp_path / "service.lock").open("a+", encoding="utf-8") as lock_file:
        with pytest.raises(BlockingIOError):
            service_lock_module._lock_file_nonblocking(lock_file)
