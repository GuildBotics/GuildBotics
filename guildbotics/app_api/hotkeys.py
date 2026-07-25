"""Global hotkey assignments for the desktop quick-run window.

Assignments live in the workspace config (``.guildbotics/config/hotkeys.yml``)
next to the commands they refer to, so they travel with the workspace instead of
being pinned to one machine. This module owns the file format, the accelerator
grammar and the conflict rules; the desktop only registers what it is told and
never decides which combination is legal.
"""

from __future__ import annotations

import re
from pathlib import Path

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import HotkeySettings
from guildbotics.utils.fileio import (
    get_primary_config_path,
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
# A bare letter or digit would be taken away from every other application.
_NEEDS_MODIFIER = re.compile(r"^[A-Z0-9]$")


def hotkeys_file() -> Path:
    """Return the workspace hotkey settings file (which may not exist)."""
    return get_primary_config_path(Path(HOTKEYS_FILE))


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
            f"'{accelerator}' is not a usable key combination.",
            context={"assignment": label, "accelerator": accelerator},
        )
    if len(set(modifiers)) != len(modifiers) or any(
        modifier not in _MODIFIERS for modifier in modifiers
    ):
        raise AppApiError(
            "hotkey_invalid",
            f"'{accelerator}' is not a usable key combination.",
            context={"assignment": label, "accelerator": accelerator},
        )
    if not modifiers and _NEEDS_MODIFIER.match(base):
        raise AppApiError(
            "hotkey_needs_modifier",
            f"'{accelerator}' needs a modifier key.",
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
                f"'{accelerator}' is already assigned to '{seen[accelerator]}'.",
                context={
                    "assignment": label,
                    "accelerator": accelerator,
                    "conflicting_assignment": seen[accelerator],
                },
            )
        seen[accelerator] = label


def load_hotkeys() -> HotkeySettings:
    """Read the workspace hotkey assignments, defaulting to none."""
    path = hotkeys_file()
    if not path.is_file():
        return HotkeySettings()
    data = load_yaml_file(path)
    if not isinstance(data, dict):
        return HotkeySettings()

    commands = data.get("commands") or {}
    if not isinstance(commands, dict):
        commands = {}
    return HotkeySettings(
        quick_run=str(data.get("quick_run") or ""),
        commands={
            str(command): str(accelerator)
            for command, accelerator in commands.items()
            if accelerator
        },
    )


def save_hotkeys(settings: HotkeySettings) -> HotkeySettings:
    """Validate and persist hotkey assignments, dropping empty entries."""
    normalized = HotkeySettings(
        quick_run=settings.quick_run.strip(),
        commands={
            command: accelerator.strip()
            for command, accelerator in settings.commands.items()
            if accelerator.strip()
        },
    )
    validate_settings(normalized)
    save_yaml_file(
        hotkeys_file(),
        {"quick_run": normalized.quick_run, "commands": normalized.commands},
    )
    return normalized
