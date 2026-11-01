from __future__ import annotations

import os
import signal
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli import service
from guildbotics.drivers.app_api_lock import PARENT_PID_ENV, START_SERVICES_ENV
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT


class Child:
    """A server process that ends with ``code``, or on ``terminate``."""

    def __init__(self, code: int = 0, on_wait=None):
        self.code = code
        self.on_wait = on_wait
        self.terminated = 0

    def terminate(self):
        self.terminated += 1

    def wait(self):
        if self.on_wait is not None:
            self.on_wait()
        return self.code


@pytest.fixture
def launched(monkeypatch, tmp_path):
    launches = []

    def launch(child: Child):
        def popen(command, *, env):
            launches.append(SimpleNamespace(command=command, env=env))
            return child

        monkeypatch.setattr(service.subprocess, "Popen", popen)
        return launches

    return launch


@pytest.mark.parametrize("services", [False, True])
def test_start_runs_the_server_even_with_no_workspace_selected(
    launched, monkeypatch, services
):
    """The server opens the selected workspace itself, or none until one is
    chosen on the screens. The services start with it only when asked;
    otherwise it follows the Service screen's setting."""
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    launches = launched(Child())
    old = signal.getsignal(signal.SIGINT)

    result = CliRunner().invoke(
        main, ["server", "start", *(["--services"] if services else [])]
    )

    assert result.exit_code == 0, result.output
    [launch] = launches
    assert launch.command == [sys.executable, "-m", "guildbotics.app_api"]
    assert GUILDBOTICS_WORKSPACE_ROOT not in launch.env
    assert (launch.env.get(START_SERVICES_ENV) == "1") is services
    assert launch.env[PARENT_PID_ENV] == str(os.getpid())
    assert signal.getsignal(signal.SIGINT) is old


@pytest.mark.parametrize("option", ["--only=events", "--max-consecutive-errors=3"])
def test_start_takes_no_service_settings_of_its_own(option):
    assert CliRunner().invoke(main, ["server", "start", option]).exit_code == 2


@pytest.mark.parametrize("command", ["start", "stop", "kill"])
def test_the_old_top_level_commands_are_gone(command):
    assert CliRunner().invoke(main, [command]).exit_code == 2


@pytest.mark.parametrize("code,exit_code", [(1, 1), (-signal.SIGKILL, 1)])
def test_a_server_that_did_not_exit_on_request_fails_the_start(
    launched, code, exit_code
):
    launched(Child(code))

    assert CliRunner().invoke(main, ["server", "start"]).exit_code == exit_code


@pytest.mark.skipif(not hasattr(signal, "raise_signal"), reason="needs raise_signal")
def test_sigterm_reaches_the_server_and_ctrl_c_is_left_to_it(launched):
    """A service manager signals the start alone; Ctrl-C reaches the server too,
    as a member of the terminal's process group, and asks it on its own."""
    child = Child()

    def signals():
        signal.raise_signal(signal.SIGINT)
        assert child.terminated == 0
        signal.raise_signal(signal.SIGTERM)

    child.on_wait = signals
    launched(child)

    result = CliRunner().invoke(main, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert child.terminated == 1
