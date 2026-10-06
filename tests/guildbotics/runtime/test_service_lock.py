from __future__ import annotations

import errno
import json
import logging
import os
import threading
import time
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


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def quick_poll(monkeypatch) -> None:
    monkeypatch.setattr(service_lock_module, "SETTING_POLL_SECONDS", 0.01)


@pytest.mark.parametrize("write", ["api", "editor"])
def test_a_held_service_follows_the_setting_however_it_is_written(
    tmp_path, workspace, quick_poll, write
) -> None:
    """The holder reads the file itself: the Desktop's API may run in another
    process, and a CLI user edits the file by hand."""
    settings = workspace / ".guildbotics" / "local" / "service.yml"

    def switch(enabled: bool) -> None:
        if write == "api":
            set_service_keeps_awake(enabled)
        else:
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(
                f"keep_awake: {str(enabled).lower()}\n", encoding="utf-8"
            )

    service_lock = ServiceLock(tmp_path / "service.lock")
    service_lock.acquire(owner="cli", workspace=workspace)
    try:
        switch(True)
        turned_on = _wait_until(lambda: wakepy.modecount() == 1)
        switch(False)
        turned_off = _wait_until(lambda: wakepy.modecount() == 0)
    finally:
        service_lock.release()

    assert (turned_on, turned_off) == (True, True)


def test_a_setting_half_written_by_hand_keeps_the_hold_until_it_reads(
    tmp_path, workspace, quick_poll, caplog
) -> None:
    set_service_keeps_awake(True)
    settings = workspace / ".guildbotics" / "local" / "service.yml"
    service_lock = ServiceLock(tmp_path / "service.lock")
    service_lock.acquire(owner="cli", workspace=workspace)
    try:
        with caplog.at_level(logging.WARNING):
            _replace(settings, "keep_awake: [")
            time.sleep(0.2)
            held_while_unreadable = wakepy.modecount()
            _replace(settings, "keep_awake: false\n")
            released = _wait_until(lambda: wakepy.modecount() == 0)
    finally:
        service_lock.release()

    warned = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("guildbotics")
    ]
    assert held_while_unreadable == 1
    assert released is True
    assert len(warned) == 1
    assert "keep-awake setting cannot be read" in warned[0]


def _replace(path: Path, text: str) -> None:
    staged = path.with_name(f"{path.name}.staged")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


def test_a_held_service_reads_the_setting_only_when_it_changes(
    tmp_path, workspace, quick_poll, monkeypatch
) -> None:
    """An open file cannot be replaced on Windows, so the holder must not keep
    the file open for the writer's save to land on."""
    reads = []
    original = service_lock_module.service_keeps_awake

    def counting(workspace=None):
        reads.append(workspace)
        return original(workspace)

    monkeypatch.setattr(service_lock_module, "service_keeps_awake", counting)
    service_lock = ServiceLock(tmp_path / "service.lock")
    service_lock.acquire(owner="cli", workspace=workspace)
    try:
        time.sleep(0.3)
        unchanged = len(reads)
        set_service_keeps_awake(True)
        followed = _wait_until(lambda: wakepy.modecount() == 1)
        time.sleep(0.3)
        changed = len(reads)
    finally:
        service_lock.release()

    # The read at acquire and the first poll, then one for the one change.
    assert unchanged == 2
    assert followed is True
    assert changed == 3


def test_a_released_service_no_longer_follows_the_setting(
    tmp_path, workspace, quick_poll
) -> None:
    service_lock = ServiceLock(tmp_path / "service.lock")
    service_lock.acquire(owner="cli", workspace=workspace)
    service_lock.release()

    set_service_keeps_awake(True)
    time.sleep(0.2)

    assert wakepy.modecount() == 0
    assert [
        thread
        for thread in threading.enumerate()
        if thread.name == "guildbotics-service-keep-awake"
    ] == []


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
