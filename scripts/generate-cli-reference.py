#!/usr/bin/env python
"""Generate docs/cli_reference.md from the Click definitions in guildbotics.cli.

The generated file is the single source of truth for "which commands and
options exist". Run this script and commit the result whenever the CLI
changes; tests/guildbotics/cli/test_cli_reference.py fails in CI when the
committed file drifts from the Click definitions.

Usage:
    uv run --no-sync python scripts/generate-cli-reference.py [--check]
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

import click

from guildbotics.cli import main as root_command

PROG_NAME = "guildbotics"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "cli_reference.md"
REGENERATE_COMMAND = (
    "uv run --no-sync python scripts/generate-cli-reference.py"
)
HEADER = f"""# GuildBotics CLI Reference

<!-- AUTO-GENERATED FILE. DO NOT EDIT BY HAND. -->

This reference is generated from the Click definitions in `guildbotics/cli/`.
To regenerate it after changing the CLI, run:

```bash
{REGENERATE_COMMAND}
```

For concepts (workspaces, custom commands, scheduling, secrets), see the
[README](../README.md) and the
[Custom Command Development Guide](custom_command_guide.en.md).
"""


def _escape_cell(text: str) -> str:
    """Escape text for use inside a one-line Markdown table cell.

    Angle brackets are escaped so placeholders like ``<owner>/<repo>`` are not
    parsed as HTML tags by GitHub.
    """
    collapsed = " ".join(text.split())
    return (
        collapsed.replace("|", "\\|").replace("<", "\\<").replace(">", "\\>")
    )


def _anchor(command_path: str) -> str:
    """GitHub-style heading anchor for a `## `command path`` heading."""
    return "#" + command_path.replace(" ", "-")


def _iter_contexts(
    command: click.Command, info_name: str, parent: click.Context | None = None
) -> list[click.Context]:
    """Depth-first list of contexts for the command and its visible subcommands.

    Each command's ``context_settings`` are applied like ``make_context`` does,
    so settings such as ``show_default`` render exactly as in ``--help``.
    """
    ctx = click.Context(
        command, info_name=info_name, parent=parent, **command.context_settings
    )
    contexts = [ctx]
    if isinstance(command, click.Group):
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            contexts.extend(_iter_contexts(sub, name, ctx))
    return contexts


def _usage_line(ctx: click.Context) -> str:
    pieces = " ".join(ctx.command.collect_usage_pieces(ctx))
    return f"{ctx.command_path} {pieces}".rstrip()


def _help_section(ctx: click.Context) -> str:
    if not ctx.command.help:
        return ""
    return inspect.cleandoc(ctx.command.help) + "\n\n"


def _options_table(ctx: click.Context) -> str:
    records = [
        record
        for param in ctx.command.get_params(ctx)
        if isinstance(param, click.Option) and not param.hidden
        if (record := param.get_help_record(ctx)) is not None
    ]
    if not records:
        return ""
    lines = ["| Option | Description |", "| --- | --- |"]
    lines += [
        f"| `{_escape_cell(opts)}` | {_escape_cell(desc) or '-'} |"
        for opts, desc in records
    ]
    return "\n".join(lines) + "\n\n"


def _subcommands_table(ctx: click.Context) -> str:
    command = ctx.command
    if not isinstance(command, click.Group):
        return ""
    rows = []
    for name in command.list_commands(ctx):
        sub = command.get_command(ctx, name)
        if sub is None or sub.hidden:
            continue
        path = f"{ctx.command_path} {name}"
        summary = _escape_cell(sub.get_short_help_str(limit=200)) or "-"
        rows.append(f"| [`{path}`]({_anchor(path)}) | {summary} |")
    if not rows:
        return ""
    lines = ["| Subcommand | Summary |", "| --- | --- |", *rows]
    return "\n".join(lines) + "\n\n"


def _command_section(ctx: click.Context) -> str:
    return (
        f"## `{ctx.command_path}`\n\n"
        + _help_section(ctx)
        + f"```text\n{_usage_line(ctx)}\n```\n\n"
        + _options_table(ctx)
        + _subcommands_table(ctx)
    )


def _command_index(contexts: list[click.Context]) -> str:
    lines = ["## Command index", "", "| Command | Summary |", "| --- | --- |"]
    for ctx in contexts:
        summary = _escape_cell(ctx.command.get_short_help_str(limit=200)) or "-"
        lines.append(
            f"| [`{ctx.command_path}`]({_anchor(ctx.command_path)}) | {summary} |"
        )
    return "\n".join(lines) + "\n\n"


def build_reference() -> str:
    contexts = _iter_contexts(root_command, PROG_NAME)
    parts = [HEADER + "\n", _command_index(contexts)]
    parts += [_command_section(ctx) for ctx in contexts]
    return "".join(parts).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/cli_reference.md from the Click definitions."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed reference differs from the generated output.",
    )
    check = parser.parse_args().check
    generated = build_reference()
    committed = OUTPUT_PATH.read_text() if OUTPUT_PATH.exists() else None
    if check:
        if committed != generated:
            print(
                f"{OUTPUT_PATH} is out of date. Regenerate it with:\n"
                f"  {REGENERATE_COMMAND}",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0
    if committed == generated:
        print(f"{OUTPUT_PATH} is already up to date.")
        return 0
    OUTPUT_PATH.write_text(generated)
    print(f"Wrote {OUTPUT_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
