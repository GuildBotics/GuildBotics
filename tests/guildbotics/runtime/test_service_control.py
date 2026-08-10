from __future__ import annotations

import json
import time

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
