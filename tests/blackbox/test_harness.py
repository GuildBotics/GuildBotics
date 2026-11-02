"""Check the isolation and failed-start teardown of the blackbox launcher."""

import ast
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests.blackbox.harness import _environment, running_stack


def test_blackbox_sources_do_not_import_guildbotics():
    for path in Path(__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            assert not any(name.split(".")[0] == "guildbotics" for name in names), (
                f"Blackbox test imports the implementation at {path}:{node.lineno}"
            )


def test_children_use_temporary_home_keyring_and_loopback_only(tmp_path, monkeypatch):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir(exist_ok=True)
    # None of these host settings may reach either the server or its CLI.
    for name in (
        "GUILDBOTICS_CONFIG_DIR",
        "GUILDBOTICS_IDENTITY_ROOT",
        "GUILDBOTICS_HOST_URL",
        "OPENAI_API_KEY",
        "MSB_HOME",
        "PYTHONSTARTUP",
    ):
        monkeypatch.setenv(name, "ambient-setting-must-not-be-inherited")
    env = _environment(tmp_path, workspace, home, 12345)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import socket
import tempfile
from pathlib import Path
import keyring

home = Path.home()
assert Path.cwd().parent == home.parent
assert Path(tempfile.gettempdir()).parent == home.parent
assert 'ambient-setting-must-not-be-inherited' not in os.environ.values()
assert type(keyring.get_keyring()).__name__ == 'FileKeyring'
keyring.set_password('blackbox-test', 'user', 'throwaway-test-value')
assert keyring.get_password('blackbox-test', 'user') == 'throwaway-test-value'
assert (home / 'blackbox-keyring.json').is_file()
keyring.delete_password('blackbox-test', 'user')
assert keyring.get_password('blackbox-test', 'user') is None
for connect in (
    lambda: socket.getaddrinfo('example.invalid', 443),
    lambda: socket.socket().connect(('192.0.2.1', 443)),
):
    try:
        connect()
    except OSError as exc:
        assert 'loopback' in str(exc)
    else:
        raise AssertionError('An external connection was allowed')
""",
        ],
        env=env,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_failed_start_reaps_the_server(tmp_path, monkeypatch):
    # Deliberately announce the wrong port: health must never be sent to an
    # unconfirmed listener, and a failed fixture must still reap its child.
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "root = Path.cwd().parent\n"
        "(root / 'pid').write_text(str(os.getpid()))\n"
        "def stopped(*args):\n"
        "    (root / 'stopped').touch()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "print('GUILDBOTICS_APP_API_PORT=wrong', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        pytest.skip("SIGTERM teardown contract uses POSIX process groups")
    monkeypatch.setenv(
        "GUILDBOTICS_BLACKBOX_SERVER", shlex.join([sys.executable, str(launcher)])
    )
    # Exercise the real fixture's failed-start path without its full timeout.
    import tests.blackbox.harness as harness

    monkeypatch.setattr(harness, "STARTUP_TIMEOUT_SECONDS", 3)
    with pytest.raises(AssertionError, match="did not announce"):
        with running_stack(tmp_path):
            pytest.fail("The wrong listen notice must not yield a stack")
    assert (tmp_path / "stopped").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)
