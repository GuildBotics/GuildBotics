from __future__ import annotations

import json
from contextlib import nullcontext
import os
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from guildbotics.app_api import server
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.server import (
    ALLOWED_ORIGINS_ENV,
    TOKEN_ENV,
    _read_allowed_origins,
    _restore_active_workspace,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.local_api import LocalApiEndpoint, endpoint_path, read_endpoint
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


@pytest.fixture(autouse=True)
def _isolate_machine_state(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


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
            "port": runner.config.port,
            "loop": runner.config.loop,
        }
        captured["endpoint"] = read_endpoint()
        captured["mode"] = endpoint_path().stat().st_mode & 0o777
        app.state.runtime.on_workspace_changed(tmp_path / "other")
        captured["changed_endpoint"] = read_endpoint()

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr(server.uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(
        server.uvicorn.Config,
        "bind_socket",
        lambda config: nullcontext(
            SimpleNamespace(getsockname=lambda: (config.host, config.port))
        ),
    )
    monkeypatch.setattr(sys, "argv", ["guildbotics-app-api"])
    monkeypatch.chdir(tmp_path)
    for key in (ALLOWED_ORIGINS_ENV, "GUILDBOTICS_APP_API_PARENT_PID"):
        monkeypatch.delenv(key, raising=False)
    return captured


def test_main_fails_when_session_token_env_is_missing(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert TOKEN_ENV in str(excinfo.value)
    assert captured_launch == {}


def test_main_passes_env_session_token_and_origins_to_create_app(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "env-token")
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "http://127.0.0.1:1421")

    server.main()

    assert captured_launch["create_app"]["session_token"] == "env-token"
    assert captured_launch["create_app"]["allowed_origins"] == ["http://127.0.0.1:1421"]
    assert captured_launch["uvicorn"]["port"] == 8765


def test_main_pins_the_standard_event_loop(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    """uvloop's socketpair stdio truncates bun/node agent CLI output on exit."""
    monkeypatch.setenv(TOKEN_ENV, "env-token")

    server.main()

    assert captured_launch["uvicorn"]["loop"] == "asyncio"


def test_main_does_not_print_the_session_token(
    monkeypatch, captured_launch: dict[str, Any], capsys
) -> None:
    """A printed token leaks into terminal scrollback, logs and screenshots."""
    monkeypatch.setenv(TOKEN_ENV, "env-token")

    server.main()

    captured = capsys.readouterr()
    assert "env-token" not in captured.out
    assert "env-token" not in captured.err


def test_main_consumes_the_session_token_env(
    monkeypatch, captured_launch: dict[str, Any]
) -> None:
    """Launch-only credentials must not linger in host subprocess environments."""
    monkeypatch.setenv(TOKEN_ENV, "env-token")

    server.main()

    assert TOKEN_ENV not in os.environ
    assert captured_launch["create_app"]["session_token"] == "env-token"


def test_discovery_is_private_tracks_workspace_and_is_removed(
    monkeypatch, captured_launch, tmp_path
):
    monkeypatch.setenv(TOKEN_ENV, "env-token")
    server.main()

    endpoint = captured_launch["endpoint"]
    assert endpoint.port == 8765
    assert endpoint.pid == os.getpid()
    assert endpoint.token == "env-token"
    assert endpoint.service_instance_id == "api-instance"
    assert endpoint.workspace == tmp_path
    if os.name != "nt":
        assert captured_launch["mode"] == 0o600
    assert captured_launch["changed_endpoint"].workspace == tmp_path / "other"
    assert not endpoint_path().exists()


def test_discovery_is_removed_even_if_server_fails(monkeypatch, captured_launch):
    monkeypatch.setenv(TOKEN_ENV, "env-token")

    def fail(*args, **kwargs):
        assert read_endpoint() is not None
        raise RuntimeError("bind failed")

    monkeypatch.setattr(server.uvicorn.Server, "run", fail)
    with pytest.raises(RuntimeError, match="bind failed"):
        server.main()
    assert not endpoint_path().exists()


def test_failed_bind_preserves_running_desktop_discovery(
    monkeypatch, captured_launch, tmp_path
):
    endpoint = LocalApiEndpoint(
        port=8765,
        token="first",
        pid=42,
        service_instance_id="running",
        workspace=tmp_path,
    )
    endpoint.publish()
    monkeypatch.setenv(TOKEN_ENV, "second")

    def fail(config):
        raise SystemExit(1)

    monkeypatch.setattr(server.uvicorn.Config, "bind_socket", fail)
    with pytest.raises(SystemExit):
        server.main()
    assert read_endpoint() == endpoint


def test_parent_watchdog_removes_discovery_before_exiting(monkeypatch, tmp_path):
    endpoint = LocalApiEndpoint(
        port=8765, token="secret", pid=42, service_instance_id="old", workspace=tmp_path
    )
    endpoint.publish()
    monkeypatch.setattr(server, "_parent_is_alive", lambda pid: False)

    def exit_process(code):
        assert not endpoint_path().exists()
        raise SystemExit(code)

    monkeypatch.setattr(server.os, "_exit", exit_process)
    with pytest.raises(SystemExit):
        server._watch_parent(100, endpoint.discard)


def test_old_server_shutdown_preserves_replacement_discovery(tmp_path):
    endpoint = LocalApiEndpoint(
        port=8765, token="secret", pid=42, service_instance_id="old", workspace=tmp_path
    )
    replacement = endpoint.model_copy(update={"service_instance_id": "new"})
    replacement.publish()
    endpoint.discard()
    assert read_endpoint() == replacement


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("http://127.0.0.1:1421", ["http://127.0.0.1:1421"]),
        (
            " http://localhost:1420 , http://127.0.0.1:1420 ,",
            ["http://localhost:1420", "http://127.0.0.1:1420"],
        ),
    ],
)
def test_read_allowed_origins_parses_the_env_list(
    monkeypatch, raw: str | None, expected: list[str]
) -> None:
    if raw is None:
        monkeypatch.delenv(ALLOWED_ORIGINS_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOWED_ORIGINS_ENV, raw)

    assert _read_allowed_origins() == expected


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
