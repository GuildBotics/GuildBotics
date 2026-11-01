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

    monkeypatch.setattr(service, "selected_workspace", lambda: tmp_path)
    return launch


def test_start_runs_the_server_of_the_selected_workspace_with_its_services(
    launched, tmp_path
):
    launches = launched(Child())
    old = signal.getsignal(signal.SIGINT)

    result = CliRunner().invoke(main, ["start"])

    assert result.exit_code == 0, result.output
    [launch] = launches
    assert launch.command == [sys.executable, "-m", "guildbotics.app_api"]
    assert launch.env[GUILDBOTICS_WORKSPACE_ROOT] == str(tmp_path)
    assert launch.env[START_SERVICES_ENV] == "1"
    assert launch.env[PARENT_PID_ENV] == str(os.getpid())
    assert signal.getsignal(signal.SIGINT) is old


@pytest.mark.parametrize("option", ["--only=events", "--max-consecutive-errors=3"])
def test_start_takes_no_settings_of_its_own(option):
    assert CliRunner().invoke(main, ["start", option]).exit_code == 2


@pytest.mark.parametrize("code,exit_code", [(1, 1), (-signal.SIGKILL, 1)])
def test_a_server_that_did_not_exit_on_request_fails_the_start(
    launched, code, exit_code
):
    launched(Child(code))

    assert CliRunner().invoke(main, ["start"]).exit_code == exit_code


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

    result = CliRunner().invoke(main, ["start"])

    assert result.exit_code == 0, result.output
    assert child.terminated == 1
