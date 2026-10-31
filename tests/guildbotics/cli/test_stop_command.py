from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli import service
from guildbotics.drivers.app_api_lock import BackendMetadata
from guildbotics.utils.i18n_tool import t


@pytest.mark.parametrize(
    "exit_stage,expected",
    [(False, [False]), (True, [False, True]), (None, [False, True, "kill"])],
)
def test_stop_escalates_through_api_then_native_termination(
    monkeypatch, exit_stage, expected
):
    alive = True
    requests = []
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata("cli", 42))
    )
    monkeypatch.setattr(service, "pid_exists", lambda pid: alive)

    def call(path, *, payload, **kwargs):
        nonlocal alive
        assert path == "/scheduler/stop"
        requests.append(payload["force"])
        if exit_stage is not None and payload["force"] is exit_stage:
            alive = False
        return {}

    monkeypatch.setattr(service, "_call", call)
    monkeypatch.setattr(
        service, "force_terminate_pid", lambda pid: requests.append("kill")
    )
    result = CliRunner().invoke(main, ["stop", "--force", "--timeout", "0"])
    assert result.exit_code == 0, result.output
    assert requests == expected


def test_stop_timeout_leaves_work_draining(monkeypatch):
    requests = []
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata("cli", 42))
    )
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)

    def call(*args, **kwargs):
        requests.append(kwargs["payload"])
        return {}

    monkeypatch.setattr(service, "_call", call)
    result = CliRunner().invoke(main, ["stop", "--timeout", "0"])
    assert result.exit_code == 0
    assert requests == [{"force": False, "wait": False}]
    assert t("runtime.service_lock.stop_timeout") in result.output


@pytest.mark.parametrize("command", ["stop", "kill"])
def test_stop_and_kill_reject_desktop_even_with_no_service(monkeypatch, command):
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata("desktop", 42))
    )
    result = CliRunner().invoke(main, [command])
    assert result.exit_code == 1
    assert t("runtime.service_lock.desktop_managed") in result.output


def test_stop_without_a_backend_is_a_noop(monkeypatch):
    monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 0
    assert t("runtime.service_lock.not_running") in result.output


def test_stop_never_terminates_without_authenticating_the_local_api(monkeypatch):
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata("cli", 42))
    )
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)

    @contextmanager
    def unavailable(**kwargs):
        assert "consent" not in kwargs
        yield None

    monkeypatch.setattr(service, "connect_local_api", unavailable)
    result = CliRunner().invoke(main, ["kill"])
    assert result.exit_code == 1
    assert t("runtime.service_lock.api_unavailable") in result.output


def test_api_call_sends_requests_only_through_the_owner_client(monkeypatch):
    sent = []

    @contextmanager
    def connect(**kwargs):
        assert "consent" not in kwargs
        transport = httpx.MockTransport(
            lambda request: (sent.append(request), httpx.Response(200, json={}))[1]
        )
        with httpx.Client(
            base_url="http://127.0.0.1:8765/api", transport=transport
        ) as client:
            yield client

    monkeypatch.setattr(service, "connect_local_api", connect)
    service._call("/scheduler/stop", payload={"force": True})
    assert sent[0].url.path == "/api/scheduler/stop"
    assert sent[0].content == b'{"force":true}'
