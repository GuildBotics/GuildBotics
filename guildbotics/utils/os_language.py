"""The language the operating system shows its user interface in."""

from __future__ import annotations

import os
import re
import subprocess
import sys

from langcodes import Language, LanguageTagError

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*")


def os_ui_language() -> Language | None:
    """Read the operating system's UI language afresh.

    macOS keeps it in its own settings; elsewhere it is the first
    of the gettext variables that names one, so an agent environment reads the
    ``LANGUAGE`` its host tells it.

    Returns:
        The UI language, or ``None`` when the operating system names none or
        names one that is not a language (``C`` / ``POSIX``).
    """
    if sys.platform == "darwin":
        tag = _macos_ui_language_tag()
    else:
        tag = _posix_ui_language_tag()
    return ui_language(tag)


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


def _macos_ui_language_tag() -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip(' \t",();')
        if _LANGUAGE_TAG.fullmatch(candidate):
            return candidate
    return None


def _posix_ui_language_tag() -> str | None:
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return value.split(":", 1)[0]
    return None
