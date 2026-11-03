"""Exercise the server's service script with the checkout and OS commands stubbed."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from guildbotics.drivers.app_api_lock import (
    KILL_AFTER_SECONDS,
    TEARDOWN_BUDGET_SECONDS,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/hub-server.sh"
pytestmark = pytest.mark.usefixtures("shell_commands")

# One stub for every command: it records its call and answers from files.
STUB = """\
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['HS_ROOT'])
with (root / 'calls').open('a', encoding='utf-8') as f:
    f.write(json.dumps([name, *args]) + '\\n')
if name == 'uname':
    print(os.environ.get('HS_OS', 'Darwin'))
elif name == 'id':
    print('1234')
elif name == 'launchctl':
    loaded = root / 'loaded'
    if args[0] == 'print':
        sys.exit(0 if loaded.exists() else 1)
    if args[0] == 'bootout':
        loaded.unlink()
    if args[0] == 'bootstrap':
        loaded.touch()
elif name == 'python' and args[0] == '-c':
    if 'inspect_backend_lock' in args[1]:
        sys.exit(0 if (root / 'running').exists() else 1)
    print((root / 'interpreter').read_text())
elif name == 'guildbotics' and args == ['server', 'stop']:
    if (root / 'stop-fails').exists():
        sys.exit(1)
    (root / 'running').unlink(missing_ok=True)
elif name == 'uv' and (root / 'new-python').exists():
    (root / 'interpreter').write_text('/python/new')
"""


class Service:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        # A checkout of its own, so the script finds the stubbed environment.
        self.repo = root / "checkout dir"
        for directory in ("scripts", "desktop"):
            (self.repo / directory).mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "scripts/hub-server.sh")
        self.tools = root / "tools"
        self.tools.mkdir()
        (self.root / "interpreter").write_text("/python/old")
        for path in (
            *(
                self.tools / name
                for name in ("launchctl", "uname", "id", "uv", "npm", "npx")
            ),
            self.repo / ".venv/bin/python",
            self.repo / ".venv/bin/guildbotics",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
            path.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.tools}{os.pathsep}/usr/bin:/bin",
            "HS_ROOT": str(root),
        }

    def run(self, *args: str, ok: bool = True, **env: str):
        (self.root / "calls").write_text("", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(self.repo / "scripts/hub-server.sh"), *args],
            env={**self.env, **env},
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert (result.returncode == 0) is ok, result.stderr
        return result

    @property
    def calls(self):
        return [
            json.loads(line) for line in (self.root / "calls").read_text().splitlines()
        ]

    @property
    def plist(self) -> Path:
        return self.home / "Library/LaunchAgents/local.guildbotics.plist"

    def mark(self, name: str) -> None:
        (self.root / name).touch()


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


def _commands(calls, *names):
    return [call for call in calls if call[0] in names]


def test_install_runs_the_checkout_server_at_login_with_this_shells_path(service):
    result = service.run("install", GUILDBOTICS_SERVER_PORT="9876")

    definition = plistlib.loads(service.plist.read_bytes())
    cli = str(service.repo / ".venv/bin/guildbotics")
    assert definition["ProgramArguments"] == [cli, "server", "start"]
    assert definition["EnvironmentVariables"] == {
        "PATH": service.env["PATH"],
        "GUILDBOTICS_SERVER_PORT": "9876",
    }
    assert definition["RunAtLoad"] is True
    # A stop asked for stays stopped; a crash starts again.
    assert definition["KeepAlive"] == {"SuccessfulExit": False}
    # SIGTERM cancels the work; launchd waits as `server stop --force` does.
    assert definition["ExitTimeOut"] == KILL_AFTER_SECONDS > TEARDOWN_BUDGET_SECONDS
    log = str(service.home / "Library/Logs/guildbotics.log")
    assert definition["StandardOutPath"] == definition["StandardErrorPath"] == log
    assert ["python", str(service.repo / "scripts/install_cli_assets.py")] in (
        service.calls
    )
    assert _commands(service.calls, "launchctl")[-2:] == [
        ["launchctl", "enable", "gui/1234/local.guildbotics"],
        ["launchctl", "bootstrap", "gui/1234", str(service.plist)],
    ]
    assert log in result.stdout


def test_install_without_a_port_leaves_the_default_to_the_server(service):
    service.run("install")

    environment = plistlib.loads(service.plist.read_bytes())["EnvironmentVariables"]
    assert environment == {"PATH": service.env["PATH"]}


def test_install_refuses_while_another_server_runs(service):
    service.mark("running")

    result = service.run("install", ok=False)

    assert "A GuildBotics server is running" in result.stderr
    assert not service.plist.exists()
    assert _commands(service.calls, "launchctl") == []


def test_installing_again_replaces_the_service_and_never_stops_a_server(service):
    service.run("install")
    service.mark("running")
    service.run("install", ok=False)
    assert ["guildbotics", "server", "stop"] not in service.calls

    (service.root / "running").unlink()
    service.run("install")

    assert _commands(service.calls, "launchctl")[-3:] == [
        ["launchctl", "bootout", "gui/1234/local.guildbotics"],
        ["launchctl", "enable", "gui/1234/local.guildbotics"],
        ["launchctl", "bootstrap", "gui/1234", str(service.plist)],
    ]


def test_restart_stops_after_the_work_and_starts_again(service):
    service.run("install")
    service.mark("running")

    service.run("restart")

    assert _commands(service.calls, "guildbotics", "launchctl")[-2:] == [
        ["guildbotics", "server", "stop"],
        ["launchctl", "kickstart", "-k", "gui/1234/local.guildbotics"],
    ]


def test_restart_does_not_start_when_the_server_does_not_stop(service):
    service.run("install")
    service.mark("running")
    service.mark("stop-fails")

    service.run("restart", ok=False)

    assert not any(
        call[1] == "kickstart" for call in _commands(service.calls, "launchctl")
    )


def test_update_rebuilds_everything_then_restarts(service):
    service.run("install")

    service.run("update")

    names = [call[0] for call in service.calls]
    assert [
        "uv",
        "sync",
        "--locked",
        "--extra",
        "test",
        "--extra",
        "dev",
    ] in service.calls
    assert ["npm", "ci"] in service.calls
    assert ["npm", "run", "build", "--workspace", "desktop"] in service.calls
    assert names.index("uv") < names.index("npm") < names.index("guildbotics")
    assert service.calls[-1] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/1234/local.guildbotics",
    ]


def test_an_update_that_changes_python_leaves_the_keychain_to_a_terminal(service):
    service.run("install")
    service.mark("new-python")

    result = service.run("update")

    assert "Always Allow" in result.stdout
    assert f"{service.repo / '.venv/bin/guildbotics'} server start" in result.stdout
    assert not any(
        call[1] == "kickstart" for call in _commands(service.calls, "launchctl")
    )


def test_uninstall_stops_and_removes_the_service(service):
    service.run("install")

    service.run("uninstall")

    assert not service.plist.exists()
    assert ["launchctl", "bootout", "gui/1234/local.guildbotics"] in service.calls


@pytest.mark.parametrize("action", ["update", "restart"])
def test_actions_on_a_service_not_installed_say_how_to_install(service, action):
    assert "Run:" in service.run(action, ok=False).stderr


def test_only_macos_is_supported(service):
    assert "macOS" in service.run("install", ok=False, HS_OS="Linux").stderr
