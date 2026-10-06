from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
import wakepy
import yaml

from guildbotics.utils import advisory_lock as advisory_lock_module
from guildbotics.runtime import service_lock as service_lock_module
from guildbotics.runtime.service_lock import (
    ServiceLock,
    ServiceLockUnavailableError,
    inspect_service_lock,
    service_keeps_awake,
    set_service_keeps_awake,
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

        with pytest.raises(ServiceLockUnavailableError) as caught:
            second.acquire(owner="desktop", workspace=tmp_path / "other")
        assert caught.value.metadata == metadata
    finally:
        first.release()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "owner": "cli",
        "pid": metadata.pid,
        "service_instance_id": metadata.service_instance_id,
        "started_at": metadata.started_at,
        "workspace": str((tmp_path / "workspace").resolve()),
    }


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


def test_service_lock_runs_cleanup_after_lock_before_metadata_publish(tmp_path) -> None:
    path = tmp_path / "service.lock"
    request_path = tmp_path / "stop-request.json"
    request_path.write_text("stale", encoding="utf-8")
    service_lock = ServiceLock(path)

    metadata = service_lock.acquire(
        owner="cli",
        workspace=tmp_path,
        before_publish=lambda: request_path.unlink(),
    )
    try:
        assert not request_path.exists()
        assert inspect_service_lock(path).metadata == metadata
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

    monkeypatch.setattr(advisory_lock_module, "_WINDOWS", True)
    monkeypatch.setattr(advisory_lock_module, "_windows_locking", FakeWindowsLocking)
    path = tmp_path / "service.lock"

    with path.open("a+", encoding="utf-8") as lock_file:
        service_lock_module._lock_file_nonblocking(lock_file)
        service_lock_module._unlock_file(lock_file)

    assert [mode for _fd, mode, _length in calls] == [
        FakeWindowsLocking.LK_NBLCK,
        FakeWindowsLocking.LK_UNLCK,
    ]
    assert all(length == 1 for _fd, _mode, length in calls)
    assert path.stat().st_size == 0


def test_windows_lock_conflict_becomes_blocking_error(monkeypatch, tmp_path) -> None:
    class BusyWindowsLocking:
        LK_NBLCK = 1

        @staticmethod
        def locking(_file_descriptor: int, _mode: int, _length: int) -> None:
            raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(advisory_lock_module, "_WINDOWS", True)
    monkeypatch.setattr(advisory_lock_module, "_windows_locking", BusyWindowsLocking)

    with (tmp_path / "service.lock").open("a+", encoding="utf-8") as lock_file:
        with pytest.raises(BlockingIOError):
            service_lock_module._lock_file_nonblocking(lock_file)


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(workspace))
    return workspace


def test_the_service_lets_the_machine_sleep_unless_set_otherwise(
    tmp_path, workspace
) -> None:
    service_lock = ServiceLock(tmp_path / "service.lock")

    service_lock.acquire(owner="cli", workspace=workspace)
    held = wakepy.modecount()
    service_lock.release()

    assert service_keeps_awake() is False
    assert held == 0


def test_the_service_keeps_the_machine_awake_while_held_when_set(
    tmp_path, workspace
) -> None:
    set_service_keeps_awake(True)
    service_lock = ServiceLock(tmp_path / "service.lock")

    service_lock.acquire(owner="cli", workspace=workspace)
    held = wakepy.modecount()
    service_lock.release()

    assert (workspace / ".guildbotics" / "local" / "service.yml").read_text(
        encoding="utf-8"
    ) == "keep_awake: true\n"
    assert held == 1
    assert wakepy.modecount() == 0


def test_a_changed_setting_applies_to_a_held_service_at_once(
    tmp_path, workspace
) -> None:
    service_lock = ServiceLock(tmp_path / "service.lock")
    service_lock.acquire(owner="desktop", workspace=workspace)
    try:
        set_service_keeps_awake(True)
        service_lock.follow_keep_awake()
        turned_on = wakepy.modecount()
        set_service_keeps_awake(False)
        service_lock.follow_keep_awake()
        turned_off = wakepy.modecount()
    finally:
        service_lock.release()

    assert (turned_on, turned_off) == (1, 0)


def test_the_setting_does_not_hold_a_service_that_is_not_running(
    tmp_path, workspace
) -> None:
    service_lock = ServiceLock(tmp_path / "service.lock")
    set_service_keeps_awake(True)

    service_lock.follow_keep_awake()

    assert wakepy.modecount() == 0


def test_an_unreadable_setting_fails_the_start_without_holding_the_lock(
    tmp_path, workspace
) -> None:
    path = tmp_path / "service.lock"
    settings = workspace / ".guildbotics" / "local" / "service.yml"
    settings.parent.mkdir(parents=True)
    settings.write_text("keep_awake: [", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        ServiceLock(path).acquire(owner="cli", workspace=workspace)

    assert inspect_service_lock(path).locked is False
    assert wakepy.modecount() == 0
