from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
COMMAND_PATH = (
    REPOSITORY_ROOT
    / "guildbotics/templates/commands/functions/get_os_ui_language.py"
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


def test_main_uses_first_macos_ui_language(command_module, monkeypatch) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "darwin")
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='(\n    "fr-FR",\n    "en-US"\n)\n'
    )
    monkeypatch.setattr(
        command_module.subprocess, "run", lambda *args, **kwargs: completed
    )

    result = command_module.main(SimpleNamespace(pipe="Hello"))

    assert result == {
        "input": "Hello",
        "language_code": "fr",
        "language_name": "français",
    }


def test_main_uses_windows_ui_language(command_module, monkeypatch) -> None:
    class GetUserDefaultUILanguage:
        restype = None

        def __call__(self) -> int:
            return 0x0409

    kernel32 = SimpleNamespace(GetUserDefaultUILanguage=GetUserDefaultUILanguage())
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    monkeypatch.setattr(
        command_module.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    result = command_module.main(SimpleNamespace(pipe="こんにちは"))

    assert result == {
        "input": "こんにちは",
        "language_code": "en",
        "language_name": "English",
    }


def test_main_uses_posix_ui_language_environment(command_module, monkeypatch) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "linux")
    monkeypatch.setenv("LANGUAGE", "ja_JP:en_US")

    result = command_module.main(SimpleNamespace(pipe="Hello"))

    assert result == {
        "input": "Hello",
        "language_code": "ja",
        "language_name": "日本語",
    }


def test_main_fails_when_ui_language_is_unavailable(
    command_module, monkeypatch
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "linux")
    for name in command_module._POSIX_LANGUAGE_ENV:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(CommandError, match="operating system UI language"):
        command_module.main(SimpleNamespace(pipe="Hello"))
