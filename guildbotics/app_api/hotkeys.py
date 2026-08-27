"""Global hotkey assignments for the desktop quick-run window.

Assignments are device-local (``.guildbotics/local/hotkeys.yml``) rather than
part of the shared workspace, because what decides a hotkey is a property of
the machine and not of the workspace: whether a combination is free depends on
that operating system's own shortcuts, on the other applications installed
there, and on the physical keyboard. A combination chosen on a Mac precisely to
avoid the system shortcuts can collide with them on Windows, and the modifier a
Mac calls Command is the Windows key there. Carrying the choice between
machines would therefore register something the user never picked.

This module owns the file format, the accelerator grammar and the conflict
rules; the desktop only registers what it is told and never decides which
combination is legal.
"""

from __future__ import annotations

import re
from pathlib import Path

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import HotkeySettings
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_workspace_local_path,
    load_yaml_file,
    save_yaml_file,
)

HOTKEYS_FILE = "hotkeys.yml"

# Mirrors the accelerator grammar accepted by the Tauri global-shortcut plugin:
# zero or more modifiers followed by exactly one base key.
_MODIFIERS = ("Control", "Alt", "Shift", "Command")
_BASE_KEY = re.compile(
    r"^(?:[A-Z0-9]|F(?:[1-9]|1\d|2[0-4])|Space|Enter|Tab|Backspace|Delete|Escape"
    r"|Home|End|PageUp|PageDown|Up|Down|Left|Right|Minus|Equal|BracketLeft"
    r"|BracketRight|Backslash|Semicolon|Quote|Comma|Period|Slash|Backquote)$"
)
# Function keys are the only combinations usable on their own; any other bare
# key would be taken away from every other application. Mirrors the recorder's
# rule in the desktop frontend.
_FUNCTION_KEY = re.compile(r"^F(?:[1-9]|1\d|2[0-4])$")


def hotkeys_file() -> Path:
    """Return this device's hotkey settings file (which may not exist)."""
    return get_workspace_local_path(HOTKEYS_FILE)


def validate_accelerator(accelerator: str, *, label: str) -> None:
    """Raise when an accelerator cannot be registered as a global shortcut.

    Args:
        accelerator: Accelerator string such as ``Control+Alt+G``.
        label: Assignment name reported back to the caller on failure.
    """
    parts = accelerator.split("+")
    base = parts[-1]
    modifiers = parts[:-1]

    if not _BASE_KEY.match(base):
        raise AppApiError(
            "hotkey_invalid",
            params={"accelerator": accelerator},
            context={"assignment": label, "accelerator": accelerator},
        )
    if len(set(modifiers)) != len(modifiers) or any(
        modifier not in _MODIFIERS for modifier in modifiers
    ):
        raise AppApiError(
            "hotkey_invalid",
            params={"accelerator": accelerator},
            context={"assignment": label, "accelerator": accelerator},
        )
    if not modifiers and not _FUNCTION_KEY.match(base):
        raise AppApiError(
            "hotkey_needs_modifier",
            params={"accelerator": accelerator},
            context={"assignment": label, "accelerator": accelerator},
        )


def validate_settings(settings: HotkeySettings) -> None:
    """Validate every assignment and reject duplicated combinations."""
    seen: dict[str, str] = {}
    assignments = [("quick_run", settings.quick_run)]
    assignments += [(command, value) for command, value in settings.commands.items()]

    for label, accelerator in assignments:
        if not accelerator:
            continue
        validate_accelerator(accelerator, label=label)
        if accelerator in seen:
            raise AppApiError(
                "hotkey_conflict",
                params={"accelerator": accelerator, "command": seen[accelerator]},
                context={
                    "assignment": label,
                    "accelerator": accelerator,
                    "conflicting_assignment": seen[accelerator],
                },
            )
        seen[accelerator] = label


def _normalized(settings: HotkeySettings) -> HotkeySettings:
    """Trim the assignments and drop the ones that hold nothing.

    Applied on both read and write so a hand-edited file cannot hand the desktop
    a blank accelerator to register.
    """
    return HotkeySettings(
        quick_run=settings.quick_run.strip(),
        commands={
            command: accelerator.strip()
            for command, accelerator in settings.commands.items()
            if accelerator.strip()
        },
    )


def load_hotkeys() -> HotkeySettings:
    """Read this device's hotkey assignments, defaulting to none."""
    try:
        path = hotkeys_file()
    except WorkspaceNotConfiguredError:
        return HotkeySettings()
    if not path.is_file():
        return HotkeySettings()
    data = load_yaml_file(path)
    if not isinstance(data, dict):
        return HotkeySettings()

    commands = data.get("commands") or {}
    if not isinstance(commands, dict):
        commands = {}
    return _normalized(
        HotkeySettings(
            quick_run=str(data.get("quick_run") or ""),
            commands={
                str(command): str(accelerator)
                for command, accelerator in commands.items()
                if accelerator
            },
        )
    )


def save_hotkeys(settings: HotkeySettings) -> HotkeySettings:
    """Validate and persist hotkey assignments, dropping empty entries."""
    normalized = _normalized(settings)
    validate_settings(normalized)
    path = hotkeys_file()
    # ``local/`` is created by whatever writes there first, and setting a hotkey
    # can be that. The shared ``config/`` this used to live in was always there
    # because setup had already written to it.
    path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(
        path,
        {"quick_run": normalized.quick_run, "commands": normalized.commands},
    )
    return normalized
