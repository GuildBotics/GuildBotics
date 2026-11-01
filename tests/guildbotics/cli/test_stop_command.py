from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli import service
from guildbotics.drivers.app_api_lock import KILL_AFTER_SECONDS, BackendMetadata
from guildbotics.utils.i18n_tool import t


class Clock:
    """Time that passes only when a wait sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def server(monkeypatch):
    """A running server whose process ends when ``state["exits_at"]`` passes."""
    clock = Clock()
    state = {"exits_at": None, "requests": [], "ports": []}
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata(42, 9876))
    )
    monkeypatch.setattr(
        service,
        "pid_exists",
        lambda pid: state["exits_at"] is None or clock.now < state["exits_at"],
    )

    def call(path, *, port, payload=None, **kwargs):
        state["requests"].append((path, payload))
        state["ports"].append(port)

    monkeypatch.setattr(service, "_call", call)
    monkeypatch.setattr(
        service, "force_terminate_pid", lambda pid: state["requests"].append("kill")
    )
    return state


def test_stop_waits_for_the_work_however_long_it_takes(server):
    server["exits_at"] = 3600.0

    result = CliRunner().invoke(main, ["server", "stop"])

    assert result.exit_code == 0, result.output
    assert server["requests"] == [("/shutdown", {"force": False})]
    assert t("runtime.service_lock.waiting_for_work") in result.output
    assert t("runtime.service_lock.stopped") in result.output


@pytest.mark.parametrize(
    "command",
    [["server", "stop"], ["service", "start"], ["service", "stop"]],
)
def test_commands_reach_the_running_server_on_the_port_it_recorded(
    server, monkeypatch, command
):
    """Not the port this shell names: the one the server holding the lock
    listens on, as a service installed with another port runs."""
    server["exits_at"] = 0.5
    monkeypatch.setenv("GUILDBOTICS_SERVER_PORT", "1111")

    result = CliRunner().invoke(main, command)

    assert result.exit_code == 0, result.output
    assert server["ports"] == [9876]


def test_service_commands_need_a_running_server(monkeypatch):
    monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
    for command in (["start"], ["stop"]):
        result = CliRunner().invoke(main, ["service", *command])
        assert result.exit_code == 1
        assert t("runtime.service_lock.not_running") in result.output


def test_a_stop_that_ends_at_once_says_nothing_of_waiting(server):
    server["exits_at"] = 0.5

    result = CliRunner().invoke(main, ["server", "stop"])

    assert t("runtime.service_lock.waiting_for_work") not in result.output
    assert t("runtime.service_lock.stopped") in result.output


def test_a_forced_stop_cancels_the_work_and_leaves_the_server_its_time(server):
    server["exits_at"] = KILL_AFTER_SECONDS - 1

    result = CliRunner().invoke(main, ["server", "stop", "--force"])

    assert result.exit_code == 0, result.output
    assert server["requests"] == [("/shutdown", {"force": True})]
    assert t("runtime.service_lock.stopped_after_cancel") in result.output


def test_a_forced_stop_kills_a_teardown_that_does_not_end(server):
    result = CliRunner().invoke(main, ["server", "stop", "--force"])

    assert result.exit_code == 0, result.output
    assert server["requests"] == [("/shutdown", {"force": True}), "kill"]
    assert t("runtime.service_lock.force_killed") in result.output


@pytest.mark.parametrize(
    "command,path,payload,message",
    [
        (["start"], "/scheduler/start", None, "services_started"),
        (["stop"], "/scheduler/stop", {"force": False}, "services_stopped"),
        (["stop", "--force"], "/scheduler/stop", {"force": True}, "services_stopped"),
    ],
)
def test_service_commands_ask_the_running_server_as_the_screen_does(
    server, command, path, payload, message
):
    result = CliRunner().invoke(main, ["service", *command])

    assert result.exit_code == 0, result.output
    assert server["requests"] == [(path, payload)]
    assert t(f"runtime.service_lock.{message}") in result.output


def test_stop_without_a_backend_is_a_noop(monkeypatch):
    monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
    result = CliRunner().invoke(main, ["server", "stop"])
    assert result.exit_code == 0
    assert t("runtime.service_lock.not_running") in result.output


def test_stop_never_terminates_without_authenticating_the_local_api(monkeypatch):
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata(42, 9876))
    )
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)

    @contextmanager
    def unavailable(**kwargs):
        assert "consent" not in kwargs
        yield None

    monkeypatch.setattr(service, "connect_local_api", unavailable)
    result = CliRunner().invoke(main, ["server", "stop", "--force"])
    assert result.exit_code == 1
    assert t("runtime.service_lock.api_unavailable") in result.output
    for command in (["start"], ["stop"]):
        result = CliRunner().invoke(main, ["service", *command])
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
    service._call("/shutdown", port=8765, payload={"force": True}, timeout=5)
    assert sent[0].url.path == "/api/shutdown"
    assert sent[0].content == b'{"force":true}'
