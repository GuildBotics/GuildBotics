"""Canonical custom-command formats and file extensions."""

from __future__ import annotations

from typing import Literal

CommandFormat = Literal["markdown", "python", "shell", "yaml"]

EXTENSION_BY_FORMAT: dict[CommandFormat, str] = {
    "markdown": ".md",
    "python": ".py",
    "shell": ".sh",
    "yaml": ".yaml",
}

FORMAT_BY_EXTENSION: dict[str, CommandFormat] = {
    extension: command_format
    for command_format, extension in EXTENSION_BY_FORMAT.items()
}
FORMAT_BY_EXTENSION[".yml"] = "yaml"
