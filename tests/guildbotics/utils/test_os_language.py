from __future__ import annotations

import subprocess

import pytest

from guildbotics.utils import os_language

_POSIX_VARIABLES = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


@pytest.fixture
def posix(monkeypatch, fake_platform) -> None:
    fake_platform(os_language, "linux")
    for name in _POSIX_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_posix_reads_the_first_gettext_variable_that_names_a_language(
    monkeypatch, posix
) -> None:
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "fr_FR@euro")
    monkeypatch.setenv("LANGUAGE", "ja_JP:en_US")

    language = os_language.os_ui_language()

    assert language is not None
    assert (language.language, language.territory) == ("ja", "JP")


@pytest.mark.parametrize("value", [None, "C.UTF-8", "POSIX", "und", "!!"], ids=repr)
def test_a_setting_that_names_no_language_is_none(
    monkeypatch, posix, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv("LANG", value)

    assert os_language.os_ui_language() is None


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(args=[], returncode=1, stdout=""),
        subprocess.TimeoutExpired(cmd="defaults", timeout=2),
    ],
    ids=["unset", "timed out"],
)
def test_macos_without_a_readable_setting_is_none(
    monkeypatch, fake_platform, outcome: object
) -> None:
    def run(*_args: object, **_kwargs: object) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fake_platform(os_language, "darwin")
    monkeypatch.setattr(os_language.subprocess, "run", run)

    assert os_language.os_ui_language() is None


def test_windows_without_its_api_is_none(monkeypatch, fake_platform) -> None:
    fake_platform(os_language, "win32")
    monkeypatch.delattr(os_language.ctypes, "windll", raising=False)

    assert os_language.os_ui_language() is None
