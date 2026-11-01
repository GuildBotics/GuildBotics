"""Exercise CLI -> server process -> real authenticated API -> lifecycle -> exit."""

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
from guildbotics.drivers.app_api_lock import inspect_backend_lock
from guildbotics.drivers.service_settings import ServiceSettings, write_service_settings
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.local_api import connect_local_api


def wait_for(predicate, process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        assert process.poll() is None, process.log_path.read_text(
            encoding="utf-8", errors="replace"
        )
        time.sleep(0.05)
    raise AssertionError(f"CLI did not reach the expected service state: {status()}")


@pytest.fixture
def cli_process(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(workspace))
    # The scheduler alone, saved as the Service screen saves it: the start
    # runs with what is saved.
    write_service_settings(
        ServiceSettings(scheduled=True, routine=True, event_queue=False)
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("GUILDBOTICS_SERVER_PORT", str(port))
    children = []
    helper = Path(__file__).with_name("backend_service_fixture.py")

    def launch(mode="normal"):
        script = (
            "import sys; from guildbotics.cli import main, service; "
            "service._backend_command=lambda: sys.argv[1:]; "
            "main(['start'])"
        )
        log_path = tmp_path / f"cli-{len(children)}.log"
        log = log_path.open("w")
        process = subprocess.Popen(
            [sys.executable, "-c", script, sys.executable, str(helper)],
            cwd=workspace,
            env={**os.environ, "SERVICE_TEST_MODE": mode},
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            # Its own process group, as a terminal gives a foreground job.
            start_new_session=os.name != "nt",
        )
        process.log_path = log_path
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


def status():
    try:
        with connect_local_api(timeout=1) as client:
            if client is None:
                return None
            return client.get("/scheduler/status").json()
    except httpx.HTTPError:
        return None


def running():
    current = status()
    return current is not None and current["scheduler"]["running"]


def log_of(process):
    return process.log_path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.windows
def test_cli_stop_ends_start_successfully_and_closes_the_server(cli_process):
    launch, _workspace = cli_process
    process = launch()
    wait_for(running, process)
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 0, result.output
    assert process.wait(timeout=15) == 0, log_of(process)
    assert not inspect_backend_lock()[0]


def test_cli_kill_ends_the_server_even_while_work_holds_it(cli_process):
    launch, _workspace = cli_process
    process = launch("hold")
    wait_for(running, process)
    result = CliRunner().invoke(main, ["kill"])
    assert result.exit_code == 0, result.output
    # Killed, not asked: an OS service starts such a server again.
    assert process.wait(timeout=15) != 0
    assert not inspect_backend_lock()[0]


@pytest.mark.parametrize("mode", ["self_stop", "start_failure"])
def test_services_that_stop_or_fail_leave_the_server_serving(cli_process, mode):
    launch, _workspace = cli_process
    process = launch(mode)
    wait_for(lambda: status() is not None, process)
    time.sleep(1)
    assert process.poll() is None, log_of(process)
    assert not running()
    assert CliRunner().invoke(main, ["stop"]).exit_code == 0
    assert process.wait(timeout=15) == 0, log_of(process)


def test_a_second_start_is_refused_with_its_reason(cli_process):
    launch, _workspace = cli_process
    first = launch()
    wait_for(running, first)
    second = launch()
    assert second.wait(timeout=30) == 1
    assert t("runtime.service_lock.already_running") in log_of(second)
    assert "Traceback" not in log_of(second)
    assert first.poll() is None and running()


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
    os.name == "nt", reason="POSIX process groups and signals; no Hub runs Windows"
)
def test_two_ctrl_c_drain_then_cancel_the_work(cli_process):
    launch, workspace = cli_process
    process = launch("hold")
    wait_for(running, process)
    os.killpg(process.pid, signal.SIGINT)
    wait_for(lambda: (workspace / "graceful").exists(), process)
    assert process.poll() is None
    os.killpg(process.pid, signal.SIGINT)
    assert process.wait(timeout=15) == 0, log_of(process)
    assert not inspect_backend_lock()[0]


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX process groups and signals; no Hub runs Windows"
)
def test_sigterm_to_start_alone_cancels_the_work_at_once(cli_process):
    """What an OS sends to stop its service, once, before it kills the job."""
    launch, workspace = cli_process
    process = launch("hold")
    wait_for(running, process)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=15) == 0, log_of(process)
    assert not (workspace / "graceful").exists()
    assert not inspect_backend_lock()[0]
