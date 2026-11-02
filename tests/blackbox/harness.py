"""Launch a replaceable server; communicate only through HTTP and CLI processes."""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

REPOSITORY = "acme/team/demo"
TRACKER = "acme/team/demo-tickets"
PERSON = "blackbox"
SUPPORT = Path(__file__).parent / "support"
ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_SECONDS = 30


@dataclass
class Stack:
    """The public interfaces of one throwaway workspace."""

    workspace: Path
    env: dict[str, str]
    api: httpx.Client

    @property
    def config_dir(self) -> str:
        return str(self.workspace / ".guildbotics/config")

    def cli(self, *arguments: str, content: str | None = None):
        result = subprocess.run(
            [sys.executable, "-m", "guildbotics.cli", *arguments],
            cwd=self.workspace,
            env=self.env,
            input=content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def member(self, *arguments: str, content: str | None = None):
        options = ["--person", PERSON, "--format", "json"]
        if content is not None:
            options.append("--content-stdin")
        return json.loads(self.cli("member", *arguments, *options, content=content))


def _yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def _seed(workspace: Path, home: Path) -> None:
    """Write only documented configuration and local-provider seed files."""
    config = workspace / ".guildbotics/config"
    _yaml(
        config / "team/project.yml",
        {
            "name": "Blackbox",
            "language": "en",
            "services": {
                "code_hosting_service": {"name": "local", "owner": "acme/team"},
                "ticket_service": {"name": "local", "owner": "acme/team"},
                "chat_service": {"name": "local"},
            },
        },
    )
    _yaml(
        home
        / f".guildbotics/identity/.guildbotics/config/team/members/{PERSON}/identity.yml",
        {
            "person_id": PERSON,
            "name": "Blackbox",
            "person_type": "agent",
            "account_info": {},
        },
    )
    _yaml(
        config / f"team/members/{PERSON}/member.yml",
        {
            "is_active": True,
            "profile": {"roles": {}},
            "routine_commands": [],
            "task_schedules": [],
        },
    )
    # No scheduled commands, patrol, or chat turns can run in this stack.
    _yaml(
        workspace / ".guildbotics/local/service.yml",
        {"scheduled": True, "routine": False, "event_queue": False},
    )
    # As in desktop/e2e/start-stack.mjs: fail before placing a runtime or
    # fetching an image when service start attempts snapshot upkeep.
    runtime = home / ".guildbotics/data/msb"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.touch()
    tracker = (
        workspace / ".guildbotics/local/services/tickets/acme%2Fteam%2Fdemo-tickets"
    )
    tracker.mkdir(parents=True)
    (tracker / "tracker.json").write_text(
        json.dumps({"repository": REPOSITORY, "labels": []}), encoding="utf-8"
    )


def _environment(root: Path, workspace: Path, home: Path, port: int) -> dict[str, str]:
    # An allowlist drops ambient credentials, command brokers, workspace,
    # identity, runtime, Python startup and config overrides together.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    temporary = root / "tmp"
    temporary.mkdir()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        TMPDIR=str(temporary),
        TMP=str(temporary),
        TEMP=str(temporary),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local/share"),
        GUILDBOTICS_WORKSPACE_ROOT=str(workspace),
        GUILDBOTICS_SERVER_PORT=str(port),
        PYTHONPATH=os.pathsep.join((str(SUPPORT), str(ROOT))),
        PYTHON_KEYRING_BACKEND="blackbox_keyring.FileKeyring",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ALLOW_PROTOCOL="file",
        HTTP_PROXY="http://127.0.0.1:1",
        HTTPS_PROXY="http://127.0.0.1:1",
        ALL_PROXY="http://127.0.0.1:1",
        NO_PROXY="localhost,127.0.0.1,::1",
    )
    return env


def _stop(process: subprocess.Popen) -> None:
    """Reap the launcher and its process group, even after failed startup."""
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    finally:
        if os.name != "nt":
            # A wrapper may have exited while a descendant still holds pipes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@contextmanager
def running_stack(tmp_path: Path):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir(exist_ok=True)
    _seed(workspace, home)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = _environment(tmp_path, workspace, home, port)
    command = shlex.split(os.environ.get("GUILDBOTICS_BLACKBOX_SERVER", "")) or [
        sys.executable,
        "-m",
        "guildbotics.app_api",
    ]
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            encoding="utf-8",
            start_new_session=os.name != "nt",
        )
        lines: queue.Queue[str] = queue.Queue()

        def read_stdout() -> None:
            for line in process.stdout:
                lines.put(line.rstrip())

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            # This is a wire contract, deliberately not imported from the server.
            notice = f"GUILDBOTICS_APP_API_PORT={port}"
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while True:
                assert process.poll() is None, "Server exited before its listen notice"
                remaining = deadline - time.monotonic()
                assert remaining > 0, "Server did not announce its port"
                try:
                    if lines.get(timeout=min(remaining, 0.1)) == notice:
                        break
                except queue.Empty:
                    pass
            # Read only after the child has proved it owns the selected port.
            token = (
                (home / f".guildbotics/data/server/{port}/owner.token")
                .read_text(encoding="utf-8")
                .strip()
            )
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}/api/",
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                timeout=1,
            ) as api:
                while True:
                    assert process.poll() is None, (
                        "Server exited before health was ready"
                    )
                    try:
                        response = api.get("health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, (
                        "Server health did not become ready"
                    )
                    time.sleep(0.05)
                api.timeout = httpx.Timeout(10)
                yield Stack(workspace, env, api)
        finally:
            _stop(process)
            reader.join(timeout=2)
            process.stdout.close()
