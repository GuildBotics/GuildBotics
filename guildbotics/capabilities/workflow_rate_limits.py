"""Shared rate-limit helpers for workflow error handling.

Extracts rate-limit information from exception chains and records
``workflow.rate_limited`` diagnostics events.  Used by both the chat
conversation workflow and the ticket-driven workflow so they share the
same event schema and extraction logic.

This module does **not** call any provider API.
"""

from __future__ import annotations

from dataclasses import dataclass

from guildbotics.capabilities.completion_retry import find_cli_agent_execution_error
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.utils.i18n_tool import t


@dataclass(frozen=True)
class WorkflowRateLimit:
    """Rate-limit details extracted from a CLI agent execution error."""

    retry_after_at: str = ""
    retry_after_text: str = ""

    @property
    def retry_after_display(self) -> str:
        return self.retry_after_text or self.retry_after_at


def workflow_rate_limit_from_exception(
    exc: BaseException,
) -> WorkflowRateLimit | None:
    """Extract rate-limit details if *exc* wraps a rate-limited CLI agent error.

    Returns ``None`` when the exception chain does not contain a
    ``CliAgentExecutionError`` with ``category="rate_limited"``.
    """
    found = find_cli_agent_execution_error(exc, category="rate_limited")
    if found is None:
        return None
    details = getattr(found, "details", {})
    return WorkflowRateLimit(
        retry_after_at=str(details.get("retry_after_at", "") or ""),
        retry_after_text=str(details.get("retry_after_text", "") or ""),
    )


def record_workflow_rate_limited(
    *,
    person_id: str,
    command: str,
    run_id: str,
    subject_id: str = "",
    source_event_id: str = "",
    retry_after: WorkflowRateLimit,
    default_source: str = "",
) -> None:
    """Record a ``workflow.rate_limited`` diagnostics event."""
    record_correlated_event(
        event_type="workflow.rate_limited",
        default_source=default_source,
        person_id=person_id,
        command=command,
        attributes={
            "error.category": "rate_limited",
            "rate_limit.retry_after_at": retry_after.retry_after_at,
            "rate_limit.retry_after_text": retry_after.retry_after_text,
        },
        payload={
            "category": "rate_limited",
            "retry_after_at": retry_after.retry_after_at,
            "retry_after_text": retry_after.retry_after_text,
            "run_id": run_id,
            "subject_id": subject_id,
            "source_event_id": source_event_id,
        },
    )


def workflow_rate_limit_notice_text(retry_after: WorkflowRateLimit) -> str:
    """Build the human-readable rate-limit notice for a ticket or chat comment."""
    display = retry_after.retry_after_display
    if display:
        return t(
            "commands.workflows.common.rate_limited_escalation_with_reset",
            retry_after=display,
        )
    return t("commands.workflows.common.rate_limited_escalation")
