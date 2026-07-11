"""Shared Click option definitions for the GuildBotics CLI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

FormatChoice = click.Choice(["json", "markdown"])


def format_option(default: str) -> Callable[[Any], Any]:
    """``--format`` option with the shared output-format help text."""
    return click.option(
        "--format",
        "output_format",
        type=FormatChoice,
        default=default,
        help="Output format.",
    )
