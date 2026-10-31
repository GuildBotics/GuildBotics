"""Smoke tests for the Local API sidecar process.

These tests launch ``python -m guildbotics.app_api`` as a real subprocess
(the same entrypoint the desktop app spawns), wait for its port notice, read
the owner token it wrote, poll ``/api/health`` until ready, and assert basic
request handling plus a clean shutdown. They are hermetic: the server listens
on a free loopback port named by ``GUILDBOTICS_SERVER_PORT`` and keeps its
records under a ``tmp_path`` home, never the real home directory.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from guildbotics.app_api.server import PORT_NOTICE_PREFIX
from guildbotics.utils.local_api import PORT_ENV

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401

STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 5.0


def _can_bind_localhost() -> bool:
    try:
        _free_port()
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


pytestmark = pytest.mark.skipif(
    not _can_bind_localhost(),
    reason="Environment cannot bind a local TCP socket.",
)


class _Sidecar:
    def __init__(
        self, base_url: str, token: str, process: subprocess.Popen[str]
    ) -> None:
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.process = process


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[_Sidecar]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    running_sidecar = _start_sidecar(tmp_path, home)
    try:
        yield running_sidecar
    finally:
        _terminate(running_sidecar.process)


def _start_sidecar(startup_dir: Path, home: Path) -> _Sidecar:
    # Fully hermetic: redirect HOME to the tmp tree and drop any inherited config
    # overrides so the subprocess never reads or writes the real ~/.guildbotics.
    # The port comes from the environment, as every launcher hands it over.
    requested = _free_port()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        PORT_ENV: str(requested),
    }
    for key in (
        "GUILDBOTICS_CONFIG_DIR",
        "GUILDBOTICS_WORKSPACE_ROOT",
    ):
        env.pop(key, None)
    process = subprocess.Popen(
        [sys.executable, "-m", "guildbotics.app_api"],
        cwd=str(startup_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # The sidecar holds the port before announcing it, as it does for Desktop.
    with ThreadPoolExecutor(max_workers=1) as reader:
        notice = reader.submit(_read_port, process)
        try:
            port = notice.result(timeout=STARTUP_TIMEOUT_SECONDS)
        except BaseException:
            _terminate(process)
            raise
    try:
        assert port == requested
        # Written before the notice: whoever reads it is the owner.
        token = (
            (home / ".guildbotics/data/server" / str(port) / "owner.token")
            .read_text(encoding="utf-8")
            .strip()
        )
        sidecar = _Sidecar(f"http://127.0.0.1:{port}", token, process)
        _wait_for_health(sidecar)
    except BaseException:
        _terminate(process)
        raise
    return sidecar


def _read_port(process: subprocess.Popen[str]) -> int:
    assert process.stdout is not None
    output = []
    for line in process.stdout:
        if line.startswith(PORT_NOTICE_PREFIX):
            return int(line.removeprefix(PORT_NOTICE_PREFIX))
        output.append(line)
    raise AssertionError(
        f"Sidecar exited before announcing its port:\n{''.join(output)}"
    )


def _drain_output(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    try:
        return process.stdout.read() or ""
    except ValueError:
        return ""


def _wait_for_health(sidecar: _Sidecar) -> None:
    process = sidecar.process
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "Sidecar exited during startup with code "
                f"{process.returncode}:\n{_drain_output(process)}"
            )
        try:
            response = httpx.get(
                f"{sidecar.base_url}/api/health",
                headers=sidecar.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == HTTP_OK:
                return
            last_error = AssertionError(
                f"Unexpected health status {response.status_code}"
            )
        except httpx.HTTPError as error:  # not yet accepting connections
            last_error = error
        time.sleep(POLL_INTERVAL_SECONDS)
    _terminate(process)
    raise AssertionError(f"Sidecar did not become healthy in time: {last_error!r}")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def test_sidecar_health_returns_ok(sidecar: _Sidecar) -> None:
    response = httpx.get(
        f"{sidecar.base_url}/api/health",
        headers=sidecar.headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_OK
    assert response.json()["status"] == "ok"
    assert response.json()["service_instance_id"]


def test_sidecar_rejects_invalid_token(sidecar: _Sidecar) -> None:
    token_id = sidecar.headers["Authorization"].split("_")[1]
    response = httpx.get(
        f"{sidecar.base_url}/api/health",
        headers={"Authorization": f"Bearer gbt_{token_id}_wrong-secret"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_UNAUTHORIZED
    assert response.json()["code"] == "unauthorized"


def test_sidecar_config_status_reports_the_command_working_directory(
    sidecar: _Sidecar, tmp_path: Path
) -> None:
    response = httpx.get(
        f"{sidecar.base_url}/api/config/status",
        headers=sidecar.headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    # Where a command runs when the screen names no directory: the exchange
    # directory under the sidecar's HOME, not where the sidecar was started.
    assert (
        Path(payload["cwd"]).resolve()
        == (tmp_path / "home/Documents/GuildBotics").resolve()
    )


def test_sidecar_restores_backend_active_workspace(tmp_path: Path) -> None:
    startup = tmp_path / "startup"
    workspace = tmp_path / "selected"
    home = tmp_path / "home"
    startup.mkdir()
    workspace.mkdir()
    home.mkdir(exist_ok=True)
    (workspace / ".guildbotics" / "config").mkdir(parents=True)
    state_path = home / ".guildbotics" / "data" / "active-workspace.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )

    running_sidecar = _start_sidecar(startup, home)
    try:
        response = httpx.get(
            f"{running_sidecar.base_url}/api/config/status",
            headers=running_sidecar.headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        assert response.status_code == HTTP_OK
        payload = response.json()
        assert Path(payload["cwd"]) == home / "Documents/GuildBotics"
        assert Path(payload["workspace"]) == workspace
        assert Path(payload["storage_dir"]) == workspace
    finally:
        _terminate(running_sidecar.process)


@pytest.mark.windows
def test_sidecar_shuts_down_cleanly(sidecar: _Sidecar) -> None:
    process = sidecar.process
    assert process.poll() is None  # still running

    process.terminate()
    return_code = process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    # A terminate-initiated shutdown should not surface as a crash. Windows has
    # no SIGTERM: `terminate()` is `TerminateProcess`, which ends the process
    # with exit code 1 whatever the process itself would have returned, so
    # there the only thing the exit code can say is that it stopped when asked.
    assert process.poll() is not None
    assert return_code in ({1} if os.name == "nt" else {0, -15})
