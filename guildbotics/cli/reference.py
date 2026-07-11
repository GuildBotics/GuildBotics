from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

CommandInfo = dict[str, Any]


def generate_cli_reference(command: click.Command, *, prog_name: str) -> str:
    """Render a deterministic Markdown reference from a Click command tree."""
    root_context = click.Context(command, info_name=prog_name, terminal_width=1000)
    command_info = command.to_info_dict(root_context)
    usages = dict(_command_usages(command, root_context, prog_name))
    entries = list(_walk_command_info(command_info, prog_name))

    lines = [
        "# CLI Reference",
        "",
        "This file is generated from the Click command definitions in "
        "`guildbotics.cli:main`.",
        "Do not edit it directly. Regenerate it with:",
        "",
        "```bash",
        "uv run --no-sync python scripts/generate-cli-reference.py",
        "```",
        "",
        "## Commands",
        "",
        "| Command | Description |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| `{path}` | {_cell(_description(info))} |" for path, info in entries
    )

    for path, info in entries:
        depth = min(path.count(" ") + 2, 6)
        lines.extend(
            [
                "",
                f"{'#' * depth} `{path}`",
                "",
                _description(info),
                "",
                "```text",
                usages[path],
                "```",
            ]
        )
        params = [
            param for param in info.get("params", []) if not param.get("hidden", False)
        ]
        if not params:
            continue
        lines.extend(
            [
                "",
                "| Parameter | Kind | Type | Required | Default | Description |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        lines.extend(_parameter_row(param) for param in params)

    return "\n".join(lines) + "\n"


def write_cli_reference(
    command: click.Command,
    output_path: Path,
    *,
    prog_name: str,
    check: bool = False,
) -> bool:
    """Write the reference, or return whether an existing file is current."""
    rendered = generate_cli_reference(command, prog_name=prog_name)
    if check:
        return output_path.is_file() and output_path.read_text() == rendered
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)
    return True


def _command_usages(
    command: click.Command,
    context: click.Context,
    path: str,
) -> Iterator[tuple[str, str]]:
    yield path, command.get_usage(context).strip()
    if not isinstance(command, click.Group):
        return
    for name in command.list_commands(context):
        child = command.get_command(context, name)
        if child is None or child.hidden:
            continue
        child_context = click.Context(
            child,
            info_name=name,
            parent=context,
            terminal_width=1000,
        )
        yield from _command_usages(child, child_context, f"{path} {name}")


def _walk_command_info(
    info: CommandInfo, path: str
) -> Iterator[tuple[str, CommandInfo]]:
    if info.get("hidden", False):
        return
    yield path, info
    for name, child in info.get("commands", {}).items():
        yield from _walk_command_info(child, f"{path} {name}")


def _parameter_row(param: CommandInfo) -> str:
    return (
        "| "
        + " | ".join(
            [
                f"`{_parameter_name(param)}`",
                _cell(param["param_type_name"]),
                _cell(_parameter_type(param)),
                "yes" if param["required"] else "no",
                _cell(_default_value(param.get("default"))),
                _cell(param.get("help") or "—"),
            ]
        )
        + " |"
    )


def _parameter_name(param: CommandInfo) -> str:
    if param["param_type_name"] == "option":
        return ", ".join([*param.get("opts", []), *param.get("secondary_opts", [])])
    name = param["name"].upper()
    return f"{name}..." if param.get("nargs") == -1 else name


def _parameter_type(param: CommandInfo) -> str:
    type_info = param["type"]
    choices = type_info.get("choices")
    if choices:
        value = " | ".join(str(choice) for choice in choices)
    else:
        value = str(type_info.get("name", "value"))
    minimum = type_info.get("min")
    maximum = type_info.get("max")
    if minimum is not None or maximum is not None:
        value += f" ({minimum if minimum is not None else '…'}..{maximum if maximum is not None else '…'})"
    if param.get("multiple"):
        value += ", repeatable"
    return value


def _default_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def _description(info: CommandInfo) -> str:
    return " ".join((info.get("help") or info.get("short_help") or "—").split())


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
