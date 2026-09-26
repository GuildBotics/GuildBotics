"""Single source of truth for the title of one execution (trace).

The activity timeline and the diagnostics trace list ask the same question --
"what is this execution working on?" -- so the answer is resolved here once,
next to the records it reads, instead of being derived again by each screen
(the same reason :mod:`trace_status` lives here).

A trace names the first PR / issue recorded inside it as its work target. That
is one rule for every workflow: the ticket workflow declares its target when
it starts, and a chat workflow acquires one the moment the member CLI reads
or touches a PR / issue on its behalf. Reads (``inspect``) declare the target
for diagnostics but are not the session's own work, so, like read-only memory
operations, they never become an activity link.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from guildbotics.utils.i18n_tool import t
from guildbotics.utils.text_utils import first_line

#: Memory reads and signals (``recall`` / ``get`` / ``touch``) change no
#: document: their payload title is what was read, not what was done.
MEMORY_READ_ONLY_ACTIONS = frozenset({"recall", "get", "touch"})
#: A ``github.work_target`` recorded by a read-only member command.
GITHUB_READ_ONLY_ACTIONS = frozenset({"inspected"})


def is_read_only_action(attributes: Mapping[str, Any]) -> bool:
    """True when the record only read something (memory or GitHub)."""
    return (
        str(attributes.get("memory.action") or "") in MEMORY_READ_ONLY_ACTIONS
        or str(attributes.get("github.action") or "") in GITHUB_READ_ONLY_ACTIONS
    )


def is_read_only_record(item: Mapping[str, Any]) -> bool:
    """True when ``item`` is a read/signal record that changed nothing."""
    attributes = item.get("attributes")
    return isinstance(attributes, Mapping) and is_read_only_action(attributes)


def first_seen_attributes(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the trace's attributes: for each key, the earliest recorded value.

    This is the one merge behind "a trace names its first target". Both
    screens call it on the trace's records in timestamp order -- never on the
    order records happened to be appended, which differs across the processes
    (workflow, member CLI) that write into one trace.
    """
    merged: dict[str, Any] = {}
    for item in records:
        attributes = item.get("attributes")
        if isinstance(attributes, Mapping):
            for key, value in attributes.items():
                merged.setdefault(key, value)
    return merged


def github_reference_label(kind: str, number: str) -> str:
    """Return the short ``PR #n`` / ``Issue #n`` reference for a GitHub item."""
    if not number:
        return ""
    if kind == "pull_request":
        return f"PR #{number}"
    if kind == "issue":
        return f"Issue #{number}"
    return f"GitHub #{number}"


#: ``(trace attributes, person_id) -> the run's recorded completion summary``.
#: The trace holds no run record; the caller that has them supplies this. It
#: is asked only when the trace names no PR / issue, since reading run
#: records costs a directory scan.
CompletionSummary = Callable[[Mapping[str, Any], str], str]


def resolve_trace_title(
    records: Iterable[Mapping[str, Any]],
    attributes: Mapping[str, Any],
    *,
    person_id: str = "",
    command: str = "",
    workflow: str = "",
    completion_summary: CompletionSummary | None = None,
    fallback: str = "",
) -> str:
    """Return the title both screens show for one trace.

    Candidates are evaluated in order and only as far as needed, so the
    completion summary lookup never runs for a trace whose target is known.

    Args:
        records: The trace's records in timestamp order.
        attributes: The trace's merged attributes (first-seen wins).
        person_id: The member the trace belongs to.
        command: The command that opened the trace.
        workflow: The workflow that opened the trace.
        completion_summary: Looks up the member's recorded completion summary
            for the run, when the caller has run records.
        fallback: What to return when nothing names the trace (its id).
    """
    records = list(records)
    candidates: tuple[Callable[[], object], ...] = (
        lambda: attributes.get("github.title"),
        lambda: first_line(
            completion_summary(attributes, person_id) if completion_summary else ""
        ),
        lambda: attributes.get("memory.title"),
        lambda: _first_payload_text(records, "title"),
        lambda: github_reference_label(
            str(attributes.get("github.kind") or ""),
            str(attributes.get("github.number") or ""),
        ),
        lambda: _trigger_label(attributes),
        lambda: _first_payload_field(records, "prompt"),
        lambda: workflow,
        lambda: command,
        lambda: attributes.get("memory.doc_id"),
        lambda: _first_payload_field(records, "brain"),
        lambda: _first_payload_field(records, "cli_agent"),
        lambda: _first_record_text(records, "type", "event", "message"),
    )
    for candidate in candidates:
        value = candidate()
        if value:
            return str(value)
    return fallback


def _trigger_label(attributes: Mapping[str, Any]) -> str:
    """Build a provider-neutral label for an event-triggered session.

    ``event.provider`` is only present on chat-triggered workflows, so its
    presence is what selects this label. It gives an in-progress chat session
    a meaningful title before its target or completion summary exists,
    instead of falling through to the raw agent prompt.
    """
    provider = str(attributes.get("event.provider") or "").strip()
    if not provider:
        return ""
    return t("observability.trace_title.chat_trigger", provider=provider.title())


def _first_payload_text(records: list[Mapping[str, Any]], key: str) -> str:
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, Mapping) or is_read_only_record(item):
            continue
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _first_payload_field(records: list[Mapping[str, Any]], key: str) -> str:
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        fields = payload.get("fields")
        if isinstance(fields, Mapping) and fields.get(key):
            return str(fields[key])
        if payload.get(key):
            return str(payload[key])
    return ""


def _first_record_text(records: list[Mapping[str, Any]], *keys: str) -> str:
    for item in records:
        for key in keys:
            value = item.get(key)
            if value:
                return str(value)
    return ""
