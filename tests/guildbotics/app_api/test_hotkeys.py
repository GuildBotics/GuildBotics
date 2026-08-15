from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.errors import AppApiError
from guildbotics.utils.workspace_sync_port import shared_relative_path
from guildbotics.app_api.hotkeys import (
    hotkeys_file,
    load_hotkeys,
    save_hotkeys,
    validate_settings,
)
from guildbotics.app_api.models import HotkeySettings
from guildbotics.utils.fileio import WorkspaceNotConfiguredError, get_template_path
from tests.guildbotics.app_api.test_api import RuntimeStub


@pytest.fixture
def local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This device's own corner of the workspace, which never leaves it."""
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    directory = tmp_path / ".guildbotics" / "local"
    directory.mkdir(parents=True)
    return directory


def test_load_returns_empty_settings_when_file_is_absent(local_dir: Path) -> None:
    settings = load_hotkeys()

    assert settings.quick_run == ""
    assert settings.commands == {}


def test_save_then_load_round_trips_assignments(local_dir: Path) -> None:
    save_hotkeys(
        HotkeySettings(quick_run="Control+Alt+G", commands={"greet": "Control+Alt+1"})
    )

    assert hotkeys_file() == local_dir / "hotkeys.yml"
    assert load_hotkeys() == HotkeySettings(
        quick_run="Control+Alt+G", commands={"greet": "Control+Alt+1"}
    )


def test_save_drops_blank_assignments(local_dir: Path) -> None:
    saved = save_hotkeys(
        HotkeySettings(quick_run="  ", commands={"greet": "", "other": " Command+1 "})
    )

    assert saved.quick_run == ""
    assert saved.commands == {"other": "Command+1"}


def test_load_drops_blank_assignments_from_a_hand_edited_file(local_dir: Path) -> None:
    # Otherwise the desktop is handed whitespace to register and reports it as
    # rejected, instead of simply treating the assignment as unset.
    (local_dir / "hotkeys.yml").write_text(
        "quick_run: '  '\ncommands:\n  greet: '  '\n  review: ' Control+Alt+1 '\n"
    )

    assert load_hotkeys() == HotkeySettings(commands={"review": "Control+Alt+1"})


def test_load_tolerates_a_malformed_file(local_dir: Path) -> None:
    (local_dir / "hotkeys.yml").write_text("commands: not-a-mapping\n")

    assert load_hotkeys() == HotkeySettings()


@pytest.mark.parametrize(
    "accelerator",
    [
        "Control+Alt+G",
        "Command+Shift+Space",
        "F5",
        "Control+Up",
        "Command+Slash",
    ],
)
def test_accepts_registrable_combinations(accelerator: str) -> None:
    validate_settings(HotkeySettings(quick_run=accelerator))


@pytest.mark.parametrize(
    ("accelerator", "code"),
    [
        ("G", "hotkey_needs_modifier"),
        ("1", "hotkey_needs_modifier"),
        # Only function keys are usable bare; anything else would be taken away
        # from every other application.
        ("Space", "hotkey_needs_modifier"),
        ("Enter", "hotkey_needs_modifier"),
        ("Escape", "hotkey_needs_modifier"),
        ("Up", "hotkey_needs_modifier"),
        ("Meta+G", "hotkey_invalid"),
        ("Control+Control+G", "hotkey_invalid"),
        ("Control+CapsLock", "hotkey_invalid"),
        ("Control+", "hotkey_invalid"),
        ("F25", "hotkey_invalid"),
    ],
)
def test_rejects_unusable_combinations(accelerator: str, code: str) -> None:
    with pytest.raises(AppApiError) as raised:
        validate_settings(HotkeySettings(quick_run=accelerator))

    assert raised.value.code == code
    assert raised.value.context["accelerator"] == accelerator


def test_rejects_a_combination_assigned_twice() -> None:
    with pytest.raises(AppApiError) as raised:
        validate_settings(
            HotkeySettings(
                quick_run="Control+Alt+G",
                commands={"greet": "Control+Alt+G"},
            )
        )

    assert raised.value.code == "hotkey_conflict"
    assert raised.value.context["assignment"] == "greet"
    assert raised.value.context["conflicting_assignment"] == "quick_run"


def test_save_rejects_conflicts_before_writing(local_dir: Path) -> None:
    with pytest.raises(AppApiError):
        save_hotkeys(
            HotkeySettings(
                quick_run="Control+Alt+G", commands={"greet": "Control+Alt+G"}
            )
        )

    assert not hotkeys_file().exists()


def test_endpoints_round_trip_and_report_conflicts(
    local_dir: Path, tmp_path: Path
) -> None:
    headers = {"X-GuildBotics-Session-Token": "secret"}
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        assert client.get("/hotkeys", headers=headers).json() == {
            "quick_run": "",
            "commands": {},
        }

        saved = client.put(
            "/hotkeys",
            headers=headers,
            json={"quick_run": "Control+Alt+G", "commands": {"greet": "Control+Alt+1"}},
        )
        assert saved.status_code == 200
        assert saved.json()["commands"] == {"greet": "Control+Alt+1"}
        assert client.get("/hotkeys", headers=headers).json()["quick_run"] == (
            "Control+Alt+G"
        )

        conflict = client.put(
            "/hotkeys",
            headers=headers,
            json={"quick_run": "Control+Alt+G", "commands": {"greet": "Control+Alt+G"}},
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "hotkey_conflict"


def test_endpoint_requires_the_session_token(local_dir: Path, tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))
    with TestClient(app) as client:
        assert client.get("/hotkeys").status_code == 401


@pytest.fixture
def no_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_load_returns_defaults_without_workspace(no_workspace: None) -> None:
    assert load_hotkeys() == HotkeySettings()


def test_save_without_workspace_fails_and_keeps_templates_pristine(
    no_workspace: None,
) -> None:
    template_file = get_template_path() / "hotkeys.yml"
    assert not template_file.exists()

    with pytest.raises(WorkspaceNotConfiguredError):
        save_hotkeys(HotkeySettings(quick_run="Control+Alt+G"))

    assert not template_file.exists()


def test_put_endpoint_reports_missing_workspace(
    no_workspace: None, tmp_path: Path
) -> None:
    headers = {"X-GuildBotics-Session-Token": "secret"}
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        assert client.get("/hotkeys", headers=headers).json() == {
            "quick_run": "",
            "commands": {},
        }

        rejected = client.put(
            "/hotkeys",
            headers=headers,
            json={"quick_run": "Control+Alt+G", "commands": {}},
        )

    assert rejected.status_code == 409
    assert rejected.json()["code"] == "workspace_not_configured"
    assert not (get_template_path() / "hotkeys.yml").exists()


def test_hotkeys_never_travel_to_another_machine(local_dir: Path) -> None:
    """Whether a combination is free depends on that machine's own shortcuts,
    other applications, and keyboard, so carrying the choice across would
    register something the user never picked."""
    save_hotkeys(HotkeySettings(quick_run="Control+Alt+G"))

    assert shared_relative_path(hotkeys_file()) is None
