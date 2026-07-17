"""Smoke tests for the Local API sidecar process.

These tests launch ``python -m guildbotics.app_api`` as a real subprocess
(the same entrypoint the desktop app spawns), poll ``/health`` until ready,
and assert basic request handling plus a clean shutdown. They are hermetic:
the server binds a dynamically chosen free port on ``127.0.0.1`` and uses a
``tmp_path`` working directory, never the real home directory.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401

STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 5.0

TOKEN = "smoke-test-token"
AUTH_HEADERS = {"X-GuildBotics-Session-Token": TOKEN}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _can_bind_localhost(),
    reason="Environment cannot bind a local TCP socket.",
)


class _Sidecar:
    def __init__(self, base_url: str, process: subprocess.Popen[str]) -> None:
        self.base_url = base_url
        self.process = process


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[_Sidecar]:
    home = tmp_path / "home"
    home.mkdir()
    running_sidecar = _start_sidecar(tmp_path, home)
    try:
        yield running_sidecar
    finally:
        _terminate(running_sidecar.process)


def _start_sidecar(startup_dir: Path, home: Path) -> _Sidecar:
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    # Fully hermetic: redirect HOME to the tmp tree and drop any inherited config
    # overrides so the subprocess never reads or writes the real ~/.guildbotics.
    env = {**os.environ, "HOME": str(home)}
    for key in (
        "GUILDBOTICS_CONFIG_DIR",
        "GUILDBOTICS_TRANSCRIPT_DETAIL",
        "GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS",
    ):
        env.pop(key, None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "guildbotics.app_api",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            TOKEN,
        ],
        cwd=str(startup_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _wait_for_health(base_url, process)
    return _Sidecar(base_url, process)


def _drain_output(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    try:
        return process.stdout.read() or ""
    except ValueError:
        return ""


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
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
                f"{base_url}/health",
                headers=AUTH_HEADERS,
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
        f"{sidecar.base_url}/health",
        headers=AUTH_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok"}


def test_sidecar_rejects_invalid_token(sidecar: _Sidecar) -> None:
    response = httpx.get(
        f"{sidecar.base_url}/health",
        headers={"X-GuildBotics-Session-Token": "wrong-token"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_UNAUTHORIZED
    assert response.json()["code"] == "invalid_session_token"


def test_sidecar_config_status_reports_working_directory(
    sidecar: _Sidecar, tmp_path: Path
) -> None:
    response = httpx.get(
        f"{sidecar.base_url}/config/status",
        headers=AUTH_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert Path(payload["cwd"]).resolve() == tmp_path.resolve()


def test_sidecar_restores_backend_active_workspace(tmp_path: Path) -> None:
    startup = tmp_path / "startup"
    workspace = tmp_path / "selected"
    home = tmp_path / "home"
    for path in (startup, workspace, home):
        path.mkdir()
    (workspace / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=selected-data\n", encoding="utf-8"
    )
    state_path = home / ".guildbotics" / "data" / "active-workspace.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )

    running_sidecar = _start_sidecar(startup, home)
    try:
        response = httpx.get(
            f"{running_sidecar.base_url}/config/status",
            headers=AUTH_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        assert response.status_code == HTTP_OK
        payload = response.json()
        assert Path(payload["cwd"]) == workspace
        assert Path(payload["workspace_data_dir"]) == workspace / "selected-data"
    finally:
        _terminate(running_sidecar.process)


def test_sidecar_shuts_down_cleanly(sidecar: _Sidecar) -> None:
    process = sidecar.process
    assert process.poll() is None  # still running

    process.terminate()
    return_code = process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    # SIGTERM-initiated shutdown should not surface as a crash.
    assert process.poll() is not None
    assert return_code in {0, -15}
