"""Check the isolation and failed-start teardown of the blackbox launcher."""

import ast
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.blackbox.environment import EnvironmentSource
from tests.blackbox.harness import PERSON, _environment, running_stack


def test_server_fixture_runs_without_cli(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from tests.blackbox import conftest

    monkeypatch.setenv("GUILDBOTICS_BLACKBOX_SERVER", "server-command")
    monkeypatch.delenv("GUILDBOTICS_BLACKBOX_CLI", raising=False)
    lifecycle = []
    instance = object()

    @contextmanager
    def server(root):
        assert root == tmp_path
        lifecycle.append("start")
        try:
            yield instance
        finally:
            lifecycle.append("stop")

    monkeypatch.setattr(conftest, "running_stack", server)
    with contextmanager(conftest.server_stack.__wrapped__)(tmp_path) as stack:
        assert stack is instance
        assert lifecycle == ["start"]
    assert lifecycle == ["start", "stop"]
    with pytest.raises(pytest.skip.Exception, match="CLI command"):
        conftest.cli_available.__wrapped__()


def test_server_fixture_requires_a_server_command(tmp_path, monkeypatch):
    from tests.blackbox import conftest

    monkeypatch.delenv("GUILDBOTICS_BLACKBOX_SERVER", raising=False)
    monkeypatch.setenv("GUILDBOTICS_BLACKBOX_CLI", "cli-command")
    with pytest.raises(pytest.skip.Exception, match="server command"):
        next(conftest.server_stack.__wrapped__(tmp_path))


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
    # As running_stack lays it out: the run directory is HOME.
    workspace, home = tmp_path / "workspace", tmp_path
    workspace.mkdir()
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

home = Path.home()
assert Path.cwd().parent == home
assert Path(tempfile.gettempdir()).parent == home
assert 'ambient-setting-must-not-be-inherited' not in os.environ.values()
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


def test_product_member_cli_keeps_binary_mismatch_guard(server_stack):
    command = shlex.split(os.environ.get("GUILDBOTICS_BLACKBOX_CLI", ""))
    if not command or not os.environ.get("GUILDBOTICS_BLACKBOX_MEMBER_CLI"):
        pytest.skip("Requires the example server's matching member client launcher")
    result = subprocess.run(
        [*command, "member", "help", "--person", PERSON],
        cwd=server_stack.workspace,
        env=server_stack.env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "CLI and server versions differ" in result.stderr


def test_server_round_trips_secrets_in_its_isolated_vault(server_stack):
    # Saving forces key creation; a second update and GET force decryption.
    # Unlike empty-store checks this fails without a working credential vault.
    api = server_stack.api
    before = api.get("config/project").json()
    for provider in ("openai", "gemini"):
        saved = api.put(
            "config/project",
            json={
                "language": before["language"],
                "config_dir": server_stack.config_dir,
                "expected_revisions": before["revisions"],
                "provider_api_keys": {provider: "synthetic-blackbox-value"},
            },
        )
        assert saved.status_code == 200, saved.text
        before = api.get("config/project").json()
        assert before["provider_api_keys"][provider] is True
    assert before["provider_api_keys"]["openai"] is True
    assert "synthetic-blackbox-value" not in str(before)
    assert "DBUS_SESSION_BUS_ADDRESS" not in server_stack.env


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


def test_environment_adoption_and_cleanup_leave_the_source_intact(tmp_path, symlinks):
    source = tmp_path / "source"
    source_home = tmp_path / "device-home"
    declaration = Path(".guildbotics/config/intelligences/agent_environment.yml")
    snapshots = Path(".guildbotics/local/agent_environment/snapshots")
    runtime = Path(".guildbotics/data/msb")
    (source / declaration).parent.mkdir(parents=True)
    (source / declaration).write_text("image: fixture\n", encoding="utf-8")
    (source / snapshots / "ready").mkdir(parents=True)
    (source / snapshots / "ready/disk").write_bytes(b"snapshot")
    (source_home / runtime).mkdir(parents=True)
    (source_home / runtime / "runtime").write_bytes(b"runtime")
    workspace, home = tmp_path / "borrowed", tmp_path / "borrowed-home"
    (home / runtime).parent.mkdir(parents=True)
    (home / runtime).touch()
    EnvironmentSource(source, source_home).adopt(workspace, home)
    assert (workspace / declaration).read_text() == "image: fixture\n"
    assert not (workspace / declaration).is_symlink()
    assert (workspace / snapshots).is_symlink()
    assert (home / runtime).is_symlink()
    assert (workspace / snapshots / "ready/disk").read_bytes() == b"snapshot"
    assert (home / runtime / "runtime").read_bytes() == b"runtime"
    shutil.rmtree(workspace)
    shutil.rmtree(home)
    assert (source / snapshots / "ready/disk").read_bytes() == b"snapshot"
    assert (source_home / runtime / "runtime").read_bytes() == b"runtime"


def test_unknown_provider_does_not_silently_run_local(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_BLACKBOX_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="Unknown blackbox provider"):
        with running_stack(tmp_path):
            pytest.fail("An unknown provider must fail before starting the server")
