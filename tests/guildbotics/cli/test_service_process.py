"""Exercise CLI -> child -> real authenticated API -> lifecycle -> CLI exit."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli.service import _call
from guildbotics.drivers.app_api_lock import inspect_backend_lock, read_backend_metadata


def wait_for(predicate, process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        assert process.poll() is None, "CLI exited before the expected service state"
        time.sleep(0.05)
    raise AssertionError("CLI did not reach the expected service state")


@pytest.fixture
def cli_process(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(workspace))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("GUILDBOTICS_SERVER_PORT", str(port))
    children = []
    helper = Path(__file__).with_name("backend_service_fixture.py")

    def launch(mode="normal"):
        script = (
            "import sys; from guildbotics.cli import main, service; "
            "service._backend_command=lambda: [sys.executable, sys.argv[1]]; "
            "main(['start','--only','scheduler'])"
        )
        log = (tmp_path / f"cli-{len(children)}.log").open("w")
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(helper)],
            cwd=workspace,
            env={**os.environ, "SERVICE_TEST_MODE": mode},
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        children.append((process, log))
        return process

    try:
        yield launch, workspace
    finally:
        for process, log in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            # Its watchdog must release the machine lock even on a forced quit.
            deadline = time.monotonic() + 10
            while inspect_backend_lock()[0] and time.monotonic() < deadline:
                time.sleep(0.1)
            log.close()
        assert not inspect_backend_lock()[0]


def running():
    try:
        return _call("/scheduler/status", timeout=1, method="GET")["scheduler"][
            "running"
        ]
    except RuntimeError, httpx.HTTPError:
        return False


@pytest.mark.windows
@pytest.mark.parametrize("command,mode", [("stop", "normal"), ("kill", "hold")])
def test_cli_stop_and_kill_end_start_successfully_and_close_the_backend(
    cli_process, command, mode
):
    launch, _workspace = cli_process
    process = launch(mode)
    wait_for(running, process)
    metadata = read_backend_metadata()
    assert metadata.owner == "cli" and metadata.pid != process.pid
    result = CliRunner().invoke(main, [command])
    assert result.exit_code == 0, result.output
    assert process.wait(timeout=15) == 0
    assert not inspect_backend_lock()[0]
    assert read_backend_metadata().stop_requested


@pytest.mark.parametrize("mode", ["self_stop", "start_failure"])
def test_service_failure_exits_nonzero_and_releases_backend_lock(cli_process, mode):
    launch, _workspace = cli_process
    process = launch(mode)
    assert process.wait(timeout=30) != 0
    assert not inspect_backend_lock()[0]


@pytest.mark.windows
def test_forced_parent_exit_is_followed_by_the_backend_watchdog(cli_process):
    launch, _workspace = cli_process
    process = launch()
    wait_for(running, process)
    process.kill()
    process.wait(timeout=5)
    deadline = time.monotonic() + 10
    while inspect_backend_lock()[0] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not inspect_backend_lock()[0]


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX foreground signals; Windows grouping is covered by the launch contract",
)
def test_two_real_signals_drain_then_cancel_the_child(cli_process):
    launch, workspace = cli_process
    process = launch("hold")
    wait_for(running, process)
    process.send_signal(signal.SIGTERM)
    wait_for(lambda: (workspace / "graceful").exists(), process)
    assert process.poll() is None
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=15) == 0
    assert not inspect_backend_lock()[0]
