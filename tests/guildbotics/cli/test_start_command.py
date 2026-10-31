from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli import service
from guildbotics.drivers.app_api_lock import (
    BackendMetadata,
    CONFLICT_NOTICE_PREFIX,
    PORT_NOTICE_PREFIX,
)
from guildbotics.utils.i18n_tool import t


def status(running=True, *, active=False, failed=False):
    return {
        "scheduler": {"running": running, "state": "failed" if failed else "running"},
        "events": {"running": False, "state": "stopped"},
        "has_active_work": active,
    }


@pytest.mark.parametrize("only", [None, "scheduler", "events"])
def test_start_maps_options_to_the_backend_and_closes_it(monkeypatch, tmp_path, only):
    process = object()
    calls = []
    monkeypatch.setattr(service, "selected_workspace", lambda: tmp_path)
    monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
    monkeypatch.setattr(
        service, "_launch_backend", lambda workspace: (process, queue.Queue())
    )
    monkeypatch.setattr(service, "_wait_for_port", lambda *args: calls.append("notice"))
    monkeypatch.setattr(
        service, "_supervise", lambda child, payload, signals: calls.append(payload)
    )
    monkeypatch.setattr(
        service,
        "_close_backend",
        lambda child, announced: calls.append((child, announced)),
    )
    old = signal.getsignal(signal.SIGINT)
    arguments = ["start", "--max-consecutive-errors", "7"]
    if only:
        arguments += ["--only", only]
    result = CliRunner().invoke(main, arguments)
    assert result.exit_code == 0, result.output
    assert calls == [
        "notice",
        {
            "sources": {
                "scheduled": only != "events",
                "routine": only != "events",
                "event_queue": only != "scheduler",
            },
            "max_consecutive_errors": 7,
        },
        (process, True),
    ]
    assert signal.getsignal(signal.SIGINT) is old


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_start_rejects_nonpositive_error_limit(limit):
    assert (
        CliRunner().invoke(main, ["start", "--max-consecutive-errors", limit]).exit_code
        == 2
    )


def test_start_does_not_launch_when_desktop_is_open(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "selected_workspace", lambda: tmp_path)
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata("desktop", 42))
    )
    result = CliRunner().invoke(main, ["start"])
    assert result.exit_code == 1
    assert t("runtime.service_lock.desktop_start") in result.output


@pytest.mark.parametrize("owner", ["cli", "desktop"])
def test_start_handles_the_race_after_its_lock_precheck(monkeypatch, owner):
    notices = queue.Queue()
    notices.put(CONFLICT_NOTICE_PREFIX + owner)
    with pytest.raises(Exception, match=service._conflict_message(owner)):
        service._wait_for_port(SimpleNamespace(poll=lambda: None), notices)


def test_two_signals_escalate_without_waiting_for_the_first_api_request(
    monkeypatch, capsys
):
    signals = []
    stages = []
    graceful = threading.Event()
    cancelled = threading.Event()
    polls = 0

    def call(path, *, payload=None, **kwargs):
        nonlocal polls
        if path == "/scheduler/start":
            return status()
        if path == "/scheduler/stop":
            stages.append(payload["force"])
            if payload["force"]:
                cancelled.set()
            else:
                graceful.set()
                assert cancelled.wait(5), (
                    "A second signal must not wait for graceful stop"
                )
            return status(False)
        polls += 1
        if polls == 1:
            signals.append(signal.SIGTERM)
        elif polls == 2:
            assert graceful.wait(5)
            signals.append(signal.SIGINT)
        elif polls >= 3:
            assert cancelled.wait(5)
            # A backend force stop may time out with a unit still marked
            # running. The CLI must then close the backend, not poll forever.
            return status(True, failed=True)
        return status()

    monkeypatch.setattr(service, "_call", call)
    monkeypatch.setattr(service, "_stop_requested", lambda pid: False)
    service._supervise(SimpleNamespace(pid=42, poll=lambda: None), {}, signals)
    assert stages == [False, True]
    output = capsys.readouterr().out
    graceful_message = t("runtime.service_lock.stopping_after_work")
    force_message = t("runtime.service_lock.cancelling")
    assert output.count(graceful_message) == 1
    assert output.count(force_message) == 1
    assert output.index(graceful_message) < output.index(force_message)


def test_interrupt_before_listening_does_not_wait_for_the_startup_deadline():
    with pytest.raises(service.StartupInterruptedError):
        service._wait_for_port(
            SimpleNamespace(poll=lambda: None), queue.Queue(), [signal.SIGINT]
        )


def test_interrupted_launch_explains_the_stop_before_exiting(capsys):
    with pytest.raises(service.StartupInterruptedError):
        service._wait_for_port(
            SimpleNamespace(poll=lambda: None), queue.Queue(), [signal.SIGINT]
        )
    assert capsys.readouterr().out.strip() == t(
        "runtime.service_lock.stopping_after_work"
    )


def test_start_completion_does_not_repeat_an_already_reported_stop(monkeypatch, capsys):
    starting = Future()
    signals = [signal.SIGINT]
    calls = 0

    def background(call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return starting
        finished = Future()
        finished.set_result(status(False))
        return finished

    monkeypatch.setattr(service, "_background", background)
    monkeypatch.setattr(
        service.time,
        "sleep",
        lambda seconds: None if starting.done() else starting.set_result(status()),
    )
    monkeypatch.setattr(service, "_call", lambda *args, **kwargs: status(False))
    monkeypatch.setattr(service, "_stop_requested", lambda pid: True)
    service._supervise(SimpleNamespace(pid=42, poll=lambda: None), {}, signals)
    assert (
        capsys.readouterr().out.count(t("runtime.service_lock.stopping_after_work"))
        == 1
    )


def test_second_interrupt_can_end_a_blocked_start_request(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_background", lambda call: Future())
    process = SimpleNamespace(
        kill=lambda: calls.append("kill"), wait=lambda: calls.append("wait")
    )
    service._supervise(process, {}, [signal.SIGINT, signal.SIGINT])
    assert calls == ["kill", "wait"]


def test_signal_notices_are_visible_even_while_start_is_blocked(monkeypatch, capsys):
    signals = [signal.SIGINT]
    monkeypatch.setattr(service, "_background", lambda call: Future())

    def interrupt_again(seconds):
        assert t("runtime.service_lock.stopping_after_work") in capsys.readouterr().out
        signals.append(signal.SIGINT)

    monkeypatch.setattr(service.time, "sleep", interrupt_again)
    process = SimpleNamespace(kill=lambda: None, wait=lambda: None, poll=lambda: None)
    service._supervise(process, {}, signals)
    assert t("runtime.service_lock.cancelling") in capsys.readouterr().out


@pytest.mark.parametrize("failed", [True, False])
def test_self_stopped_or_failed_service_is_an_error(monkeypatch, failed):
    monkeypatch.setattr(
        service, "_call", lambda *args, **kwargs: status(False, failed=failed)
    )
    monkeypatch.setattr(service, "_stop_requested", lambda pid: False)
    with pytest.raises(service.BackendUnavailableError):
        service._supervise(SimpleNamespace(pid=42, poll=lambda: None), {}, [])


def test_start_waits_for_manual_work_to_drain_after_the_workers_exit(monkeypatch):
    states = iter([status(False, active=True), status(False)])
    seen = []
    monkeypatch.setattr(service, "_stop_requested", lambda pid: True)

    def call(path, **kwargs):
        seen.append(path)
        return next(states)

    monkeypatch.setattr(service, "_call", call)
    service._supervise(SimpleNamespace(pid=42, poll=lambda: None), {}, [])
    assert seen == ["/scheduler/start", "/scheduler/status"]


def test_start_failure_still_closes_its_child(monkeypatch, tmp_path):
    closed = []
    process = object()
    monkeypatch.setattr(service, "selected_workspace", lambda: tmp_path)
    monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
    monkeypatch.setattr(
        service, "_launch_backend", lambda workspace: (process, queue.Queue())
    )
    monkeypatch.setattr(service, "_wait_for_port", lambda *args: None)

    def fail(*args):
        raise service.BackendUnavailableError("sync failed")

    monkeypatch.setattr(service, "_supervise", fail)
    monkeypatch.setattr(
        service, "_close_backend", lambda child, announced: closed.append(child)
    )
    result = CliRunner().invoke(main, ["start"])
    assert result.exit_code == 1
    assert closed == [process]


@pytest.mark.windows
def test_launch_passes_parent_and_workspace_to_an_independent_child(
    monkeypatch, tmp_path
):
    script = (
        "import os, time; "
        "from pathlib import Path; "
        "Path(os.environ['GUILDBOTICS_WORKSPACE_ROOT'], 'launch').write_text("
        "os.environ['GUILDBOTICS_APP_API_PARENT_PID'] + ':' + os.environ['GUILDBOTICS_APP_API_OWNER']); "
        "print('GUILDBOTICS_APP_API_PORT=8765', flush=True); time.sleep(30)"
    )
    monkeypatch.setattr(
        service, "_backend_command", lambda: [sys.executable, "-c", script]
    )
    monkeypatch.setenv("GUILDBOTICS_SERVER_PORT", "8765")
    process, notices = service._launch_backend(tmp_path)
    try:
        service._wait_for_port(process, notices)
        assert (tmp_path / "launch").read_text() == f"{os.getpid()}:cli"
        if os.name != "nt":
            assert os.getpgid(process.pid) != os.getpgrp()
    finally:
        service._close_backend(process, announced=False)
    assert process.poll() is not None


def test_development_backend_command_uses_the_current_interpreter():
    assert service._backend_command() == [sys.executable, "-m", "guildbotics.app_api"]


def test_packaged_backend_is_next_to_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "guildbotics"))
    suffix = ".exe" if os.name == "nt" else ""
    assert service._backend_command() == [
        str(tmp_path / ("guildbotics-app-api" + suffix))
    ]


def test_cleanup_forces_a_backend_that_exceeds_its_teardown_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        service, "_call", lambda *args, **kwargs: {"teardown_budget_seconds": 60}
    )

    def wait(timeout=None):
        calls.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("backend", timeout)

    process = SimpleNamespace(
        poll=lambda: None, wait=wait, kill=lambda: calls.append("kill"), stdout=None
    )
    service._close_backend(process, announced=True)
    assert calls == [65, "kill", None]
