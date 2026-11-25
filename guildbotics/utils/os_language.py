"""The language the operating system shows its user interface in."""

from __future__ import annotations

import os

from langcodes import Language, LanguageTagError


def os_ui_language() -> Language | None:
    """Read the operating system's UI language afresh.

    Use the first gettext variable that names one. The host sets
    ``LANGUAGE`` for the guest.

    Returns:
        The UI language, or ``None`` when the operating system names none or
        names one that is not a language (``C`` / ``POSIX``).
    """
    return ui_language(_posix_ui_language_tag())


def ui_language(tag: str | None) -> Language | None:
    """The language the operating system's ``tag`` for it names.

    Returns:
        The language, or ``None`` when ``tag`` is empty or names no language.
    """
    if not tag:
        return None
    try:
        language = Language.get(tag.split(".", 1)[0].split("@", 1)[0])
    except LanguageTagError:
        return None
    return language if language.language else None


def _posix_ui_language_tag() -> str | None:
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return value.split(":", 1)[0]
    return None
