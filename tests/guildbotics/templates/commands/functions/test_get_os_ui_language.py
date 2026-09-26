from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.utils import os_language

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
COMMAND_PATH = (
    REPOSITORY_ROOT / "guildbotics/templates/commands/functions/get_os_ui_language.py"
)


@pytest.fixture
def command_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_get_os_ui_language_command", COMMAND_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def linux(monkeypatch, fake_platform) -> None:
    """The command as it runs in an agent environment, told ``LANGUAGE``."""
    fake_platform(os_language, "linux")
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)


def test_main_attaches_the_ui_language_to_the_input(
    command_module, monkeypatch, linux
) -> None:
    monkeypatch.setenv("LANGUAGE", "ja_JP")

    result = command_module.main(SimpleNamespace(pipe="Hello"))

    assert result == {
        "input": "Hello",
        "language_code": "ja",
        "language_name": "日本語",
    }


def test_main_fails_when_ui_language_is_unavailable(command_module, linux) -> None:
    with pytest.raises(CommandError, match="operating system UI language"):
        command_module.main(SimpleNamespace(pipe="Hello"))
