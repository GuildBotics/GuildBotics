from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from guildbotics.app_api import server
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.server import _restore_active_workspace
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.local_api import PORT_ENV, owner_token_path
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    active_workspace_file,
    write_active_workspace,
)


def _isolate_runtime_environment(monkeypatch) -> None:
    for key in (
        GUILDBOTICS_CONFIG_DIR,
        GUILDBOTICS_WORKSPACE_ROOT,
        "WORKSPACE_MARKER",
    ):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)


def test_restore_active_workspace_applies_backend_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    workspace = tmp_path / "selected"
    startup.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    write_active_workspace(workspace)
    active_workspace_file().with_name("workspaces.json").unlink()

    restored = _restore_active_workspace()
    AppRuntime(
        EventBus(),
        load_workspace_environment=True,
    )

    assert restored == workspace
    assert Path.cwd() == workspace
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
        workspace / ".guildbotics" / "config"
    )
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(workspace)
    from guildbotics.utils.workspace_state import registered_workspaces

    assert workspace in registered_workspaces()


@pytest.mark.parametrize("problem", ["overlap", "registry", "link"])
def test_main_recovers_invalid_selection_without_admitting_workspace(
    captured_launch, monkeypatch, tmp_path, problem, symlinks
):
    from guildbotics.utils.workspace_state import REGISTERED_WORKSPACES_FILE

    _isolate_runtime_environment(monkeypatch)
    selected = tmp_path / "home/Documents/GuildBotics/team"
    selected.mkdir(parents=True)
    write_active_workspace(selected)
    if problem == "registry":
        active_workspace_file().with_name(REGISTERED_WORKSPACES_FILE).write_text("{")
    elif problem == "link":
        selected.rename(selected.with_name("real"))
        selected.symlink_to(selected.with_name("real"), target_is_directory=True)
    server.main()
    assert captured_launch["create_app"]["workspace_problem"]
    assert GUILDBOTICS_WORKSPACE_ROOT not in os.environ
    assert GUILDBOTICS_CONFIG_DIR not in os.environ


def test_explicit_restore_registers_selected_workspace(tmp_path, monkeypatch):
    from guildbotics.utils.workspace_state import registered_workspaces

    selected = tmp_path / "selected"
    selected.mkdir()
    monkeypatch.chdir(tmp_path)
    _isolate_runtime_environment(monkeypatch)
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(selected))
    assert _restore_active_workspace() == selected
    assert selected in registered_workspaces()


@pytest.mark.parametrize(
    "file",
    [
        "config/intelligences/cli_agent_filesystem_grants.yml",
        "local/cli_agent_filesystem_grants.yml",
    ],
)
def test_malformed_grants_do_not_prevent_desktop_startup(
    captured_launch, monkeypatch, tmp_path, file
):
    monkeypatch.chdir(tmp_path)
    _isolate_runtime_environment(monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    write_active_workspace(selected)
    path = selected / ".guildbotics" / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("documents: [")
    server.main()
    assert captured_launch["create_app"]["workspace_problem"] == ""
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(selected)
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(selected / ".guildbotics/config")


def test_restored_runtime_switches_workspace_root(tmp_path: Path, monkeypatch) -> None:
    startup = tmp_path / "startup"
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    for path in (startup, selected, other):
        path.mkdir()
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    write_active_workspace(selected)

    _restore_active_workspace()
    runtime = AppRuntime(
        EventBus(),
        load_workspace_environment=True,
    )
    runtime.set_workspace(other)

    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(other.resolve())


@pytest.fixture
def captured_launch(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """Run ``main`` without binding a port, capturing what it hands the app."""
    captured: dict[str, Any] = {}

    def fake_create_app(**kwargs: Any) -> Any:
        captured["create_app"] = kwargs
        return SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(
                    system_service_run_id="api-instance",
                    get_config_status=lambda: SimpleNamespace(workspace=tmp_path),
                )
            )
        )

    def fake_run(runner: Any, **kwargs: Any) -> None:
        app = runner.config.app
        captured["uvicorn"] = {
            "app": app,
            "host": runner.config.host,
            "port": runner.config.port,
            "loop": runner.config.loop,
        }
        app.state.request_shutdown()
        captured["should_exit"] = runner.should_exit

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr(server.uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(
        server,
        "_bind",
        lambda port: nullcontext(
            SimpleNamespace(
                getsockname=lambda: ("127.0.0.1", port),
                listen=lambda backlog: None,
            )
        ),
    )
    monkeypatch.setattr(
        server, "_announce_port", lambda port: captured.update(announced=port)
    )
    monkeypatch.chdir(tmp_path)
    for key in (PORT_ENV, "GUILDBOTICS_APP_API_PARENT_PID"):
        monkeypatch.delenv(key, raising=False)
    return captured


def test_main_serves_the_credentials_of_its_port_on_loopback(
    captured_launch: dict[str, Any],
) -> None:
    server.main()

    auth = captured_launch["create_app"]["auth"]
    assert auth.port == 8765
    assert owner_token_path(8765).is_file()
    assert captured_launch["uvicorn"]["host"] == "127.0.0.1"
    assert captured_launch["uvicorn"]["port"] == 8765
    assert captured_launch["announced"] == 8765


def test_main_listens_on_the_port_this_machine_names(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    """The server, its clients and the Desktop read the same variable."""
    monkeypatch.setenv(PORT_ENV, "9876")

    server.main()

    assert captured_launch["uvicorn"]["port"] == 9876
    assert captured_launch["create_app"]["auth"].port == 9876
    assert captured_launch["announced"] == 9876


def test_main_lets_the_app_ask_the_server_to_exit(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    server.main()

    assert captured_launch["should_exit"] is True


def test_main_holds_backend_lock_until_the_server_exits(monkeypatch, captured_launch):
    from guildbotics.drivers.app_api_lock import (
        inspect_backend_lock,
        read_backend_metadata,
    )

    def run(runner, **kwargs):
        locked, metadata = inspect_backend_lock()
        assert locked and metadata.owner == "desktop"
        runner.config.app.state.on_service_stop()
        assert read_backend_metadata().stop_requested is True

    monkeypatch.setattr(server.uvicorn.Server, "run", run)
    server.main()
    assert inspect_backend_lock() == (False, None)


def test_losing_backend_never_binds_or_opens_server_records(
    monkeypatch, captured_launch
):
    from guildbotics.drivers.app_api_lock import (
        BackendAlreadyRunningError,
        hold_backend_lock,
    )

    def bind(port):
        pytest.fail("A second backend must not bind")

    monkeypatch.setattr(server, "_bind", bind)
    with hold_backend_lock():
        with pytest.raises(BackendAlreadyRunningError):
            server.main()
    assert captured_launch == {}


def test_main_pins_the_standard_event_loop(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    """uvloop's socketpair stdio truncates bun/node agent CLI output on exit."""
    server.main()

    assert captured_launch["uvicorn"]["loop"] == "asyncio"


def test_main_does_not_print_the_owner_token(
    captured_launch: dict[str, Any], capsys
) -> None:
    """A printed token leaks into terminal scrollback, logs and screenshots."""
    server.main()

    token = owner_token_path(8765).read_text("utf-8").strip()
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_main_announces_the_port_it_bound_once_it_listens(monkeypatch, captured_launch):
    """The launcher addresses the port only once its own child holds it."""
    listening: list[int] = []
    monkeypatch.setattr(
        server,
        "_bind",
        lambda port: nullcontext(
            SimpleNamespace(
                getsockname=lambda: ("", 54321),
                listen=listening.append,
            )
        ),
    )
    announced: list[int] = []

    def announce(port: int) -> None:
        # A port bound but not yet listening can still be taken on Linux.
        assert listening
        # The launcher reads the owner token of the port once told.
        assert owner_token_path(port).is_file()
        # The launcher's deadline must not count a keychain prompt, which
        # loading the workspace's secrets in create_app can wait on.
        assert "create_app" not in captured_launch
        announced.append(port)

    monkeypatch.setattr(server, "_announce_port", announce)

    server.main()

    assert announced == [54321]
    assert captured_launch["create_app"]["auth"].port == 54321


def test_the_port_notice_is_the_only_line_stdout_ever_carries():
    script = textwrap.dedent(
        """
        import subprocess, sys
        from guildbotics.app_api.server import _announce_port
        print("unterminated", end="")
        _announce_port(4321)
        print("later output")
        subprocess.run([sys.executable, "-c", "print('child output')"], check=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [
        "unterminated",
        f"{server.PORT_NOTICE_PREFIX}4321",
    ]
    assert "later output" in result.stderr and "child output" in result.stderr


def test_failed_bind_leaves_the_running_servers_credentials_alone(
    monkeypatch, captured_launch
):
    """Only the process holding the port touches the credentials of the server
    on it, so a second start cannot end the running server's sessions."""
    started: list[int] = []
    monkeypatch.setattr(server, "ServerAuth", started.append)

    def fail(port):
        raise OSError("in use")

    monkeypatch.setattr(server, "_bind", fail)
    with pytest.raises(OSError):
        server.main()
    assert started == []
    assert captured_launch == {}


def test_parent_watchdog_exits_once_the_parent_is_gone(monkeypatch):
    monkeypatch.setattr(server, "_parent_is_alive", lambda pid: False)

    def exit_process(code):
        raise SystemExit(code)

    monkeypatch.setattr(server.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as exited:
        server._watch_parent(100)
    assert exited.value.code == 0


def test_restore_active_workspace_prefers_explicit_root_over_active(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicit GUILDBOTICS_WORKSPACE_ROOT wins over active-workspace.json,
    matching the CLI resolution order."""
    startup = tmp_path / "startup"
    explicit = tmp_path / "explicit"
    active = tmp_path / "active"
    for path in (startup, explicit, active):
        path.mkdir()
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    write_active_workspace(active)
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(explicit))

    restored = _restore_active_workspace()

    assert restored == explicit.resolve()
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(explicit.resolve())
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
        explicit.resolve() / ".guildbotics" / "config"
    )


def test_restore_active_workspace_keeps_startup_cwd_when_unconfigured(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)

    restored = _restore_active_workspace()

    assert restored == startup
    assert Path.cwd() == startup


def test_restore_active_workspace_keeps_startup_cwd_when_target_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    path = active_workspace_file()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"workspace": str(tmp_path / "missing")}), encoding="utf-8"
    )

    restored = _restore_active_workspace()

    assert restored == startup
    assert Path.cwd() == startup


@pytest.mark.windows
def test_a_port_another_server_holds_cannot_be_bound_again():
    """Two servers never answer on one port, whatever the platform lets a
    reusable socket do."""
    with server._bind(0) as held:
        held.listen()
        with pytest.raises(OSError):
            server._bind(held.getsockname()[1])
