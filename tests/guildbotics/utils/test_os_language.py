from __future__ import annotations

import pytest

from guildbotics.utils import os_language

_POSIX_VARIABLES = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


@pytest.fixture
def posix(monkeypatch) -> None:
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
