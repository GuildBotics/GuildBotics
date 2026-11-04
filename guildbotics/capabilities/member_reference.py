"""Read the member reference data shared with the Rust host."""

from __future__ import annotations

import re
from collections.abc import Mapping

from guildbotics.utils.fileio import get_template_path

_DATA = get_template_path() / "member"


def capability_reference_text() -> str:
    """Read the mode-independent member reference as Markdown."""
    return (_DATA / "reference.md").read_text(encoding="utf-8").strip()


def _command_path(usage: str) -> str:
    names: list[str] = []
    for token in usage.removeprefix("guildbotics member").split():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", token):
            break
        names.append(token)
    return " ".join(names)


_COMMAND_SUMMARIES = {
    _command_path(usage): purpose
    for usage, purpose in re.findall(
        r"^- `(guildbotics member [^`]+)` — (.+)$",
        capability_reference_text(),
        flags=re.MULTILINE,
    )
}


def command_summaries() -> Mapping[str, str]:
    """Return the catalog's command purposes, keyed by command path."""
    return _COMMAND_SUMMARIES


def command_summary(command_path: str) -> str:
    """Return one command purpose, rejecting paths absent from the catalog."""
    try:
        return _COMMAND_SUMMARIES[command_path]
    except KeyError:
        raise KeyError(
            f"'guildbotics member {command_path}' has no entry in the member "
            "capability catalog (templates/member/reference.md)."
        ) from None
