"""Read-only queries over recorded GuildBotics diagnostics.

The troubleshooting assistant investigates failures by running these commands,
so they must never mutate the workspace. The store is opened for reading only:
``start_maintenance()`` is deliberately not called, because that is what
rotates and prunes the transcripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from guildbotics.cli._options import (
    apply_workspace_option,
    format_option,
    workspace_option,
)
from guildbotics.observability.diagnostics_store import DiagnosticsStore

# Traces recorded by the Desktop assistants themselves. They run as ordinary
# manual work, so the command label is what identifies them. They are hidden by
# default so the troubleshooting agent neither reads its own conversation back
# nor reports it as a failure worth investigating.
ASSISTANT_COMMAND_PREFIXES = ("author:", "troubleshoot:")

RECORD_KINDS = ("event", "log", "io", "memory")

# Assistant traces are dropped after the store returns its results, so the scan
# has to cover everything the store holds. Truncating first would let recent
# assistant turns eat the whole page and report no executions at all.
STORE_SCAN_LIMIT = 100000


@click.group()
@workspace_option
def diagnostics(workspace_dir: Path | None) -> None:
    """Read recorded diagnostics for a workspace (read-only)."""
    apply_workspace_option(workspace_dir)


_format_option = format_option("json")
_limit_option = click.option(
    "--limit", type=click.IntRange(min=1, max=1000), default=50, help="Maximum entries."
)


@diagnostics.command(name="traces")
@click.option("--source", help="Only traces from this execution source.")
@click.option("--person", "person_id", help="Only traces recorded for this member.")
@click.option(
    "--query",
    help="Free-text filter over each execution's summary and full transcript.",
)
@click.option("--attr-key", help="Only traces carrying this attribute key.")
@click.option("--attr-value", help="Required value for --attr-key.")
@_limit_option
@click.option(
    "--include-assistant/--no-include-assistant",
    default=False,
    help="Include the Desktop AI assistants' own traces.",
)
@_format_option
def traces_cmd(
    source: str | None,
    person_id: str | None,
    query: str | None,
    attr_key: str | None,
    attr_value: str | None,
    limit: int,
    include_assistant: bool,
    output_format: str,
) -> None:
    """List recorded executions, newest first."""
    filters = {
        "source": source,
        "person_id": person_id,
        "query": query,
        "attr_key": attr_key,
        "attr_value": attr_value,
    }
    summaries = _store().list_traces(
        **filters,
        limit=limit if include_assistant else STORE_SCAN_LIMIT,
        # Investigating a recurrence means searching what the failure actually
        # said, and that text lives in the transcript rather than the index.
        include_transcripts=True,
    )
    if not include_assistant:
        summaries = [item for item in summaries if not _is_assistant_trace(item)][
            :limit
        ]
    _emit({"traces": summaries}, output_format)


@diagnostics.command(name="trace")
@click.argument("trace_id")
@click.option(
    "--kind",
    "kinds",
    type=click.Choice(RECORD_KINDS),
    multiple=True,
    help="Only records of these kinds. Repeat to combine.",
)
@click.option("--level", help="Only log records at this level, such as 'error'.")
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=5000),
    default=200,
    help="Maximum records, keeping the newest.",
)
@_format_option
def trace_cmd(
    trace_id: str,
    kinds: tuple[str, ...],
    level: str | None,
    limit: int,
    output_format: str,
) -> None:
    """Show one execution's summary and records."""
    store = _store()
    records = store.get_records(trace_id)
    if kinds:
        records = [item for item in records if item.get("kind") in kinds]
    if level:
        wanted = level.casefold()
        records = [
            item for item in records if str(item.get("level", "")).casefold() == wanted
        ]
    _emit(
        {
            "trace_id": trace_id,
            "summary": store.get_summary(trace_id),
            "record_count": len(records),
            "records": records[-limit:],
        },
        output_format,
    )


@diagnostics.command(name="system")
@_limit_option
@_format_option
def system_cmd(limit: int, output_format: str) -> None:
    """Show service-wide records that belong to no single execution."""
    store = _store()
    _emit(
        {
            "trace_id": store.latest_system_trace_id() or "",
            "records": store.global_records(limit=limit),
        },
        output_format,
    )


def _store() -> DiagnosticsStore:
    """Open the workspace's diagnostics store for reading."""
    return DiagnosticsStore()


def _is_assistant_trace(summary: dict[str, Any]) -> bool:
    """Report whether a trace was recorded by a Desktop AI assistant."""
    return str(summary.get("command", "")).startswith(ASSISTANT_COMMAND_PREFIXES)


def _emit(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return
    click.echo(_to_markdown(payload))


def _to_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"## {key} ({len(value)})")
            lines.extend(
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in value
            )
        else:
            lines.append(f"## {key}")
            lines.append(
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            )
    return "\n".join(lines)
