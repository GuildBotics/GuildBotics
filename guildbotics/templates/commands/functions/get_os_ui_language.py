from __future__ import annotations

from langcodes import Language

from guildbotics.commands.errors import CommandError
from guildbotics.runtime.context import Context
from guildbotics.utils.os_language import os_ui_language


def main(context: Context) -> dict[str, str]:
    """Preserve piped input and attach the operating system UI language.

    Args:
        context: Current command execution context.

    Returns:
        Structured translation input for the parent Markdown command.
    """
    language = os_ui_language()
    language_code = language.language if language else None
    if not language_code:
        raise CommandError("Unable to determine the operating system UI language.")
    return {
        "input": context.pipe,
        "language_code": language_code,
        "language_name": Language.get(language_code).display_name(language_code),
    }
