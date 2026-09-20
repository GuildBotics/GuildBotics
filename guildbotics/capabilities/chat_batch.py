"""Input membership delivered to an agent during a chat run."""

from typing import Any

from guildbotics.capabilities.task_runs import RunStore

# Local preparation (e.g. git_commit) and chat_noop do not fulfill new input.
_CHAT_ACTION_EVIDENCE_TYPES = (
    (RunStore.CHAT_WRITE_EVIDENCE_TYPES - {"chat_noop"})
    | RunStore.TICKET_WRITE_EVIDENCE_TYPES
    | {"git_push", "issue_update"}
)


def chat_batch_event_ids(evidence: list[dict[str, Any]]) -> list[str]:
    """Include checked updates; reset membership when a retry starts a new batch."""
    event_ids: dict[str, None] = {}
    for record in evidence:
        kind = record.get("evidence_type")
        if kind == "chat_batch":
            event_ids.clear()
        if kind in {"chat_batch", "chat_updates"}:
            event_ids.update(dict.fromkeys(record["payload"]["event_ids"]))
    return list(event_ids)


def completed_chat_event_ids(evidence: list[dict[str, Any]], status: str) -> list[str]:
    """Acknowledge extra input only after a subsequent chat or publication action.

    Reading an update is not completing it. A blocked run retains all updates;
    a successful run retains updates delivered after its last external action.
    The original batch keeps its existing terminal-completion semantics.
    """
    batch: dict[str, None] = {}
    updates: dict[str, None] = {}
    handled: dict[str, None] = {}
    for record in evidence:
        kind = record.get("evidence_type")
        if kind == "chat_batch":
            batch = dict.fromkeys(record["payload"]["event_ids"])
            updates.clear()
            handled.clear()
        elif kind == "chat_updates":
            updates.update(dict.fromkeys(record["payload"]["event_ids"]))
        elif kind in _CHAT_ACTION_EVIDENCE_TYPES:
            handled.update(updates)
    if status in {"done", "asking"}:
        batch.update(handled)
    return list(batch)
