from __future__ import annotations

import ctypes
import locale
import os
import re
import subprocess
import sys
from typing import Any

from langcodes import Language, LanguageTagError

from guildbotics.commands.errors import CommandError
from guildbotics.runtime.context import Context

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*")
_POSIX_LANGUAGE_ENV = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def main(context: Context) -> dict[str, str]:
    """Preserve piped input and attach the operating system UI language.

    Args:
        context: Current command execution context.

    Returns:
        Structured translation input for the parent Markdown command.
    """
    language = _language_from_tag(_get_ui_language_tag())
    language_code = language.language
    if not language_code:
        raise CommandError("Unable to determine the operating system UI language.")
    return {
        "input": context.pipe,
        "language_code": language_code,
        "language_name": Language.get(language_code).display_name(language_code),
    }


def _get_ui_language_tag() -> str:
    if sys.platform == "darwin":
        tag = _get_macos_ui_language_tag()
    elif sys.platform == "win32":
        tag = _get_windows_ui_language_tag()
    else:
        tag = _get_posix_ui_language_tag()
    if not tag:
        raise CommandError("Unable to determine the operating system UI language.")
    return tag


def _get_macos_ui_language_tag() -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip(' \t",();')
        if _LANGUAGE_TAG.fullmatch(candidate):
            return candidate
    return None


def _get_windows_ui_language_tag() -> str | None:
    try:
        windll: Any = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        kernel32 = windll.kernel32
        get_language = kernel32.GetUserDefaultUILanguage
        get_language.restype = ctypes.c_ushort
        return locale.windows_locale.get(int(get_language()))
    except (AttributeError, OSError):
        return None


def _get_posix_ui_language_tag() -> str | None:
    for name in _POSIX_LANGUAGE_ENV:
        value = os.environ.get(name)
        if value:
            return value.split(":", 1)[0]
    return None


def _language_from_tag(tag: str) -> Language:
    normalized = tag.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    if normalized.upper() in {"C", "POSIX"}:
        raise CommandError("Unable to determine the operating system UI language.")
    try:
        language = Language.get(normalized)
    except LanguageTagError as exc:
        raise CommandError(f"Invalid operating system UI language tag: {tag}") from exc
    if not language.language or language.language == "und":
        raise CommandError(f"Invalid operating system UI language tag: {tag}")
    return language
