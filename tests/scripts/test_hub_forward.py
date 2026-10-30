"""Exercise the POSIX installer with OS commands replaced on PATH."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/hub-forward.sh"
pytestmark = pytest.mark.usefixtures("shell_commands")

STUB = """\
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['HF_ROOT'])
with (root / 'calls').open('a', encoding='utf-8') as f:
    f.write(json.dumps([name, *args]) + '\\n')
if name == 'uname':
    print(os.environ['HF_OS'])
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
elif name == 'ssh':
    sys.exit(0)
if os.environ.get('HF_FAIL') == name + ' ' + ' '.join(args):
    sys.exit(7)
"""


class Installer:
    def __init__(self, root: Path, os_name: str):
        self.root = root
        self.os_name = os_name
        self.home = root / "home space '&%$東京"
        self.home.mkdir()
        self.tools = root / "tools space '&%$"
        self.tools.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
            "HF_ROOT": str(root),
            "HF_OS": os_name,
            "SSH_AUTH_SOCK": "/must/not/inherit",
        }
        for name in ("launchctl", "systemctl", "ssh", "uname", "id"):
            path = self.tools / name
            path.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
            path.chmod(0o755)

    def run(self, *args: str, ok: bool = True):
        (self.root / "calls").write_text("", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(SCRIPT), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if ok:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
        return result

    @property
    def calls(self):
        return [
            json.loads(line) for line in (self.root / "calls").read_text().splitlines()
        ]

    @property
    def service(self):
        if self.os_name == "Darwin":
            return self.home / "Library/LaunchAgents/local.hub-forward.plist"
        return self.home / "config/systemd/user/hub-forward.service"

    def definition(self):
        if self.os_name == "Darwin":
            return plistlib.loads(self.service.read_bytes())
        return dict(
            line.split("=", 1)
            for line in self.service.read_text().splitlines()
            if "=" in line
        )

    def ssh_args(self):
        definition = self.definition()
        if self.os_name == "Darwin":
            return definition["ProgramArguments"]
        # Decode the subset of systemd syntax emitted here. Actual ssh is a stub.
        return [
            word.replace("%%", "%").replace("$$", "$")
            for word in shlex.split(definition["ExecStart"])[1:]
        ]


@pytest.fixture(params=["Darwin", "Linux"])
def installer(tmp_path, request):
    return Installer(tmp_path, request.param)


def test_install_update_and_remove(installer):
    installer.run(
        "install",
        "--hub",
        "user@hub-alias",
        "--port",
        "12345",
        "--port",
        "23456",
        "--port",
        "12345",
    )
    args = installer.ssh_args()
    assert args[0] == str(installer.tools / "ssh")
    assert args[-2:] == ["--", "user@hub-alias"]
    assert args.count("-N") == 1
    for option in (
        "ExitOnForwardFailure=yes",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ControlMaster=no",
        "ControlPath=none",
    ):
        assert args[args.index(option) - 1] == "-o"
    forwards = [args[i + 1] for i, arg in enumerate(args) if arg == "-L"]
    assert forwards == [
        "127.0.0.1:12345:127.0.0.1:12345",
        "127.0.0.1:23456:127.0.0.1:23456",
    ]
    if installer.os_name == "Linux":
        definition = installer.definition()
        assert definition["Restart"] == "always"
        assert definition["RestartSec"] == "15"
        assert definition["StartLimitIntervalSec"] == "0"
        assert definition["Environment"] == '"SSH_AUTH_SOCK="'
        assert "IdentityAgent=none" in args
        assert installer.calls[-3:] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "hub-forward.service"],
            ["systemctl", "--user", "restart", "hub-forward.service"],
        ]
    else:
        definition = installer.definition()
        assert definition["KeepAlive"] is True
        assert definition["RunAtLoad"] is True
        assert definition["ThrottleInterval"] == 15
        assert definition["StandardErrorPath"] == str(
            installer.home / "Library/Logs/hub-forward.log"
        )
        assert installer.calls[-2:] == [
            ["launchctl", "enable", "gui/1234/local.hub-forward"],
            ["launchctl", "bootstrap", "gui/1234", str(installer.service)],
        ]
    subprocess.run(args, env=installer.env, check=True, timeout=10)
    assert installer.calls[-1] == ["ssh", *args[1:]]
    original = installer.service.read_bytes()
    installer.run(
        "install", "--hub", "user@hub-alias", "--port", "12345", "--port", "23456"
    )
    assert installer.service.read_bytes() == original
    installer.run("install", "--hub", "next-hub", "--port", "34567")
    assert installer.ssh_args()[-4:] == [
        "-L",
        "127.0.0.1:34567:127.0.0.1:34567",
        "--",
        "next-hub",
    ]
    assert "23456" not in installer.service.read_text()
    if installer.os_name == "Darwin":
        assert ["launchctl", "bootout", "gui/1234/local.hub-forward"] in installer.calls
    installer.run("status")
    assert installer.calls[-1] == (
        ["launchctl", "print", "gui/1234/local.hub-forward"]
        if installer.os_name == "Darwin"
        else ["systemctl", "--user", "status", "hub-forward.service"]
    )
    installer.run("uninstall")
    assert not installer.service.exists()
    if installer.os_name == "Linux":
        assert installer.calls[-2:] == [
            ["systemctl", "--user", "disable", "--now", "hub-forward.service"],
            ["systemctl", "--user", "daemon-reload"],
        ]
    installer.run("uninstall")
    assert not (installer.home / ".ssh").exists()


def test_agent_socket_is_explicit_and_replaced(tmp_path):
    installer = Installer(tmp_path, "Linux")
    socket = str(installer.home / 'agent " % socket')
    installer.run("install", "--hub", "hub", "--port", "1", "--agent-socket", socket)
    definition = installer.definition()
    assert (
        definition["Environment"]
        == '"SSH_AUTH_SOCK=' + socket.replace('"', '\\"').replace("%", "%%") + '"'
    )
    assert "IdentityAgent=SSH_AUTH_SOCK" in installer.ssh_args()
    installer.run("install", "--hub", "hub", "--port", "65535")
    assert installer.definition()["Environment"] == '"SSH_AUTH_SOCK="'
    assert "IdentityAgent=none" in installer.ssh_args()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--hub", "hub"],
        ["--port", "12345"],
        ["--hub", "-oProxyCommand=evil", "--port", "12345"],
        ["--hub", "hub; touch bad", "--port", "12345"],
        ["--hub", "hub", "--port", "0"],
        ["--hub", "hub", "--port", "65536"],
        ["--hub", "hub", "--port", "1e3"],
        ["--hub", "hub", "--port", "001"],
        ["--hub", "hub", "--port", "999999999999999999999999999"],
        ["--hub", "hub", "--port"],
        ["--hub", "hub", "--port", "1", "--unknown", "x"],
        ["--hub", "hub", "--port", "1", "--agent-socket", "relative"],
    ],
)
def test_rejects_invalid_input_without_changing_service(installer, args):
    installer.run("install", "--hub", "hub", "--port", "10000")
    before = installer.service.read_bytes()
    installer.run("install", *args, ok=False)
    assert installer.service.read_bytes() == before
    assert not any(
        call[0] in ("systemctl", "launchctl", "ssh") for call in installer.calls
    )


def test_command_failure_is_visible(installer):
    installer.run("install", "--hub", "hub", "--port", "12345")
    installer.env["HF_FAIL"] = (
        "launchctl bootout gui/1234/local.hub-forward"
        if installer.os_name == "Darwin"
        else "systemctl --user restart hub-forward.service"
    )
    installer.run("install", "--hub", "hub", "--port", "12345", ok=False)


def test_script_has_no_application_specific_defaults():
    for extension in ("sh", "ps1"):
        text = SCRIPT.with_suffix(f".{extension}").read_text().lower()
        assert "guildbotics" not in text
        assert "branvil" not in text
        assert "7878" not in text


def test_systemd_accepts_generated_definition(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze is not installed")
    installer = Installer(tmp_path, "Linux")
    installer.run("install", "--hub", "hub", "--port", "12345")
    result = subprocess.run(
        [analyzer, "verify", str(installer.service)],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
