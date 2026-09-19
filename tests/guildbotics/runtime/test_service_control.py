from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from guildbotics.runtime import service_control
from guildbotics.runtime.service_control import (
    ServiceControlWatcher,
    StopRequest,
    clear_stop_request,
    read_stop_request,
    write_stop_request,
)


def test_write_stop_request_is_atomic(monkeypatch, tmp_path) -> None:
    path = tmp_path / "stop-request.json"
    replacements: list[tuple[object, object]] = []
    real_replace = service_control.os.replace

    def replace(source, target) -> None:
        replacements.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(service_control.os, "replace", replace)

    request = write_stop_request("service-1", "graceful", path)

    assert request == StopRequest("service-1", "graceful")
    assert read_stop_request(path) == request
    assert replacements and replacements[0][1] == path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "service_instance_id": "service-1",
        "stage": "graceful",
    }


def test_read_holds_control_lock_until_file_is_closed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "stop-request.json"
    write_stop_request("service-1", "graceful", path)
    read_started = threading.Event()
    release_read = threading.Event()
    replace_started = threading.Event()
    reader_result: list[StopRequest | None] = []
    writer_result: list[StopRequest] = []
    failures: list[BaseException] = []
    real_read_text = Path.read_text
    real_replace = os.replace

    def blocking_read_text(target: Path, *args, **kwargs) -> str:
        if threading.current_thread().name == "service-control-reader":
            read_started.set()
            assert release_read.wait(timeout=1)
        return real_read_text(target, *args, **kwargs)

    def observed_replace(source, target) -> None:
        replace_started.set()
        real_replace(source, target)

    def read() -> None:
        try:
            reader_result.append(read_stop_request(path))
        except BaseException as exc:
            failures.append(exc)

    def write() -> None:
        try:
            writer_result.append(write_stop_request("service-1", "cancel", path))
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(Path, "read_text", blocking_read_text)
    monkeypatch.setattr(service_control.os, "replace", observed_replace)
    reader = threading.Thread(target=read, name="service-control-reader")
    writer = threading.Thread(target=write, name="service-control-writer")

    reader.start()
    assert read_started.wait(timeout=1)
    writer.start()
    try:
        assert not replace_started.wait(timeout=0.1)
    finally:
        release_read.set()
        reader.join(timeout=1)
        writer.join(timeout=1)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert reader_result == [StopRequest("service-1", "graceful")]
    assert writer_result == [StopRequest("service-1", "cancel")]


def test_stop_request_stage_never_downgrades(tmp_path) -> None:
    path = tmp_path / "stop-request.json"

    write_stop_request("service-1", "cancel", path)
    result = write_stop_request("service-1", "graceful", path)

    assert result.stage == "cancel"
    assert read_stop_request(path) == StopRequest("service-1", "cancel")


def test_new_service_instance_replaces_stale_request(tmp_path) -> None:
    path = tmp_path / "stop-request.json"
    write_stop_request("old", "cancel", path)

    write_stop_request("new", "graceful", path)

    assert read_stop_request(path) == StopRequest("new", "graceful")
    clear_stop_request(path)
    assert not path.exists()


def test_watcher_accepts_only_matching_instance_and_escalates(tmp_path) -> None:
    path = tmp_path / "stop-request.json"
    calls: list[bool] = []
    watcher = ServiceControlWatcher(
        "service-1",
        lambda *, cancel: calls.append(cancel),
        path=path,
        poll_seconds=0.005,
    )
    watcher.start()
    try:
        write_stop_request("stale", "cancel", path)
        time.sleep(0.02)
        assert calls == []

        write_stop_request("service-1", "graceful", path)
        deadline = time.monotonic() + 1
        while calls != [False] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert calls == [False]

        write_stop_request("service-1", "cancel", path)
        deadline = time.monotonic() + 1
        while calls != [False, True] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert calls == [False, True]
    finally:
        watcher.close()


def test_watcher_repeatedly_receives_monotonic_stage_updates(tmp_path) -> None:
    for attempt in range(100):
        path = tmp_path / str(attempt) / "stop-request.json"
        calls: list[bool] = []
        watcher = ServiceControlWatcher(
            f"service-{attempt}",
            lambda *, cancel: calls.append(cancel),
            path=path,
            poll_seconds=0.0005,
        )
        watcher.start()
        try:
            write_stop_request(f"service-{attempt}", "graceful", path)
            deadline = time.monotonic() + 1
            while calls != [False] and time.monotonic() < deadline:
                time.sleep(0.001)
            assert calls == [False]

            write_stop_request(f"service-{attempt}", "cancel", path)
            deadline = time.monotonic() + 1
            while calls != [False, True] and time.monotonic() < deadline:
                time.sleep(0.001)
            assert calls == [False, True]
        finally:
            watcher.close()


def test_watcher_recovers_after_control_lock_timeout(monkeypatch, tmp_path) -> None:
    path = tmp_path / "stop-request.json"
    calls: list[bool] = []
    monkeypatch.setattr(service_control, "_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service_control, "_LOCK_RETRY_SECONDS", 0.001)
    watcher = ServiceControlWatcher(
        "service-1",
        lambda *, cancel: calls.append(cancel),
        path=path,
        poll_seconds=0.001,
    )

    try:
        with service_control._request_lock(path):
            watcher.start()
            time.sleep(0.05)

        write_stop_request("service-1", "graceful", path)
        deadline = time.monotonic() + 1
        while calls != [False] and time.monotonic() < deadline:
            time.sleep(0.001)
        assert calls == [False]
    finally:
        watcher.close()
