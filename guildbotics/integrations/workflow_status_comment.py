"""Workflow status comment for GitHub issue comments.

Generates and parses ``guildbotics-workflow-status-v1`` fenced blocks
embedded in GitHub issue comment bodies.  The ticket selection logic in
:mod:`~guildbotics.integrations.github.github_ticket_manager` uses
:func:`parse_workflow_status_comment` and
:func:`suppresses_ticket_selection` to decide whether a ticket should
be re-selected after a workflow error or rate limit.

This module does **not** call the GitHub API; it only builds and parses
markdown strings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from guildbotics.integrations.chat_workflow_status import (
    WORKFLOW_STATUS_KIND,
    WORKFLOW_STATUS_ROUTING_SUPPRESS,
)

WORKFLOW_STATUS_CODE_BLOCK = "guildbotics-workflow-status-v1"
WORKFLOW_STATUS_HEADING = "**GuildBotics workflow status**"

_FENCE_PATTERN = re.compile(
    r"```" + re.escape(WORKFLOW_STATUS_CODE_BLOCK) + r"\s*\n(.*?)\n```",
    re.DOTALL,
)


@dataclass(frozen=True)
class WorkflowStatusComment:
    """Parsed workflow status extracted from a GitHub comment body."""

    reason: str
    routing: str
    person_id: str
    run_id: str
    subject_id: str = ""
    retry_after_at: str = ""
    retry_after_text: str = ""


def workflow_status_comment_payload(
    *,
    reason: str,
    person_id: str,
    run_id: str,
    subject_id: str = "",
    retry_after_at: str = "",
    retry_after_text: str = "",
) -> dict[str, object]:
    """Build the JSON payload for a workflow status fenced block."""
    payload: dict[str, object] = {
        "kind": WORKFLOW_STATUS_KIND,
        "routing": WORKFLOW_STATUS_ROUTING_SUPPRESS,
        "reason": reason,
        "person_id": person_id,
        "run_id": run_id,
    }
    if retry_after_at:
        payload["retry_after_at"] = retry_after_at
    if retry_after_text:
        payload["retry_after_text"] = retry_after_text
    if subject_id:
        payload["subject_id"] = subject_id
    return payload


def render_workflow_status_comment(*, body: str, payload: dict[str, object]) -> str:
    """Render a human-readable comment with an embedded status block."""
    json_line = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (
        f"{WORKFLOW_STATUS_HEADING}\n"
        f"\n"
        f"```{WORKFLOW_STATUS_CODE_BLOCK}\n"
        f"{json_line}\n"
        f"```\n"
        f"\n"
        f"{body}"
    )


def parse_workflow_status_comment(body: str) -> WorkflowStatusComment | None:
    """Extract a :class:`WorkflowStatusComment` from a comment body.

    Returns ``None`` if the body does not contain a valid
    ``guildbotics-workflow-status-v1`` fenced block, or if the JSON is
    malformed or has ``kind`` other than :data:`WORKFLOW_STATUS_KIND`.
    """
    match = _FENCE_PATTERN.search(body)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") != WORKFLOW_STATUS_KIND:
        return None
    reason = data.get("reason")
    routing = data.get("routing")
    person_id = data.get("person_id")
    run_id = data.get("run_id")
    if not isinstance(reason, str) or not isinstance(routing, str):
        return None
    return WorkflowStatusComment(
        reason=reason,
        routing=routing,
        person_id=str(person_id) if person_id is not None else "",
        run_id=str(run_id) if run_id is not None else "",
        subject_id=str(data.get("subject_id", "")),
        retry_after_at=str(data.get("retry_after_at", "")),
        retry_after_text=str(data.get("retry_after_text", "")),
    )


def suppresses_ticket_selection(
    status: WorkflowStatusComment,
    *,
    now: datetime | None = None,
) -> bool:
    """Decide whether *status* should prevent a ticket from being selected.

    Rules:

    * ``reason == "rate_limited"`` with a future or missing
      ``retry_after_at`` → suppress.
    * ``reason == "rate_limited"`` with a past ``retry_after_at`` → do
      **not** suppress (rate limit has expired).
    * ``reason == "failed"`` → suppress (until a human comments).
    * Any other reason → do not suppress.
    """
    if status.routing != WORKFLOW_STATUS_ROUTING_SUPPRESS:
        return False

    if status.reason == "rate_limited":
        if not status.retry_after_at:
            return True
        try:
            retry_at = datetime.fromisoformat(status.retry_after_at)
        except (ValueError, TypeError):
            # Un-parseable → treat as unknown reset time → suppress.
            return True
        if retry_at.tzinfo is None:
            # Offset-naive → treat as parse failure → suppress.
            return True
        current = now or datetime.now(UTC)
        return retry_at > current

    return status.reason == "failed"
