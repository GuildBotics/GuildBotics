"""Drive a workflow agent turn until its run records completion."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from guildbotics.intelligences.common import find_cli_agent_execution_error
from guildbotics.utils.i18n_tool import t


class RunLedger(Protocol):
    """The run record a completion-managed turn reads and reports to.

    The implementation belongs to the host, which alone decides where the run
    record lives: the turn names a run, never a location.
    """

    def require_completion(self, run_id: str) -> None:
        """Raise unless the run has recorded a terminal completion."""

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        """Return the evidence the run has recorded so far."""

    def record_completed(self, run_id: str, attempt: int) -> None:
        """Record that the run's completion was found after an attempt."""

    def record_completion_missing(
        self, run_id: str, attempt: int, max_attempts: int, error: str
    ) -> None:
        """Record an attempt that ended without the run's completion."""


class CompletionRetryExhausted(Exception):
    """Raised when the agent never recorded a terminal completion in the budget."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(
            f"Agent did not complete after {attempts} attempt(s): {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


async def run_agent_turn(
    *,
    invoke: Callable[[dict[str, Any], dict[str, str]], Awaitable[Any]],
    execution_context: Mapping[str, Any],
    ledger: RunLedger,
) -> Any:
    """Drive one logical workflow turn until it records completion.

    Both ticket and chat turns share this boundary; each workflow chooses its
    attempt budget through ``max_completion_attempts``.

    ``invoke`` receives the attempt's execution context and the prompt
    parameters derived from the run record for that attempt: a chat turn gets
    ``previous_attempt_evidence``, the actions its run has already taken,
    re-read before every attempt so a session that had to be recreated does
    not repeat them. These parameters replace any the workflow passed under
    the same name.

    Returns:
        The response of the attempt after which the run was complete.

    Raises:
        CompletionRetryExhausted: When the attempt budget is exhausted. The
            caller is responsible for reporting the failure to the requester.
    """
    context = dict(execution_context)
    run_id = str(context.get("run_id") or "").strip()
    work_kind = str(context.get("work_kind") or "").strip()
    if not run_id:
        raise ValueError("Agent execution context requires a run_id.")
    if work_kind not in {"ticket", "chat"}:
        raise ValueError(
            "Completion-managed agent turns require work_kind 'ticket' or 'chat'."
        )

    attempts = _positive_int(context.get("max_completion_attempts"), 1)
    first_attempt = _positive_int(context.get("attempt"), 1)
    retry_invoke_exceptions = bool(context.get("retry_invoke_exceptions", True))
    last_error: Exception = RuntimeError("no attempts were made")

    for offset in range(attempts):
        # Agent attempts are logical; diagnostics count this dispatch's attempts.
        logical_attempt = first_attempt + offset
        dispatch_attempt = offset + 1
        turn_context = {
            **context,
            "attempt": logical_attempt,
            "resume_policy": (
                context.get("resume_policy", "fresh") if offset == 0 else "auto"
            ),
            "continuation_input": _continuation_input(context),
        }
        try:
            response = await invoke(turn_context, _turn_parameters(ledger, context))
        except Exception as exc:
            _raise_rate_limit(exc)
            if retry_invoke_exceptions:
                last_error = exc
                continue
            raise

        try:
            ledger.require_completion(run_id)
        except Exception as exc:
            _raise_rate_limit(exc)
            ledger.record_completion_missing(
                run_id, dispatch_attempt, attempts, str(exc)
            )
            last_error = exc
            continue

        ledger.record_completed(run_id, dispatch_attempt)
        return response

    raise CompletionRetryExhausted(attempts, last_error)


def _turn_parameters(ledger: RunLedger, context: Mapping[str, Any]) -> dict[str, str]:
    if context.get("work_kind") != "chat":
        return {}
    evidence = [
        item
        for item in ledger.evidence(str(context["run_id"]))
        if item["evidence_type"] != "chat_batch"
    ]
    return {
        "previous_attempt_evidence": json.dumps(
            evidence, ensure_ascii=False, sort_keys=True
        )
    }


def _continuation_input(context: Mapping[str, Any]) -> str:
    run_id = str(context.get("run_id") or "")
    if context.get("work_kind") == "chat":
        return t(
            "commands.workflows.common.agent_chat_continuation",
            run_id=run_id,
            event_id=str(context.get("event_id") or ""),
        )
    return t("commands.workflows.common.agent_continuation", run_id=run_id)


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _raise_rate_limit(exc: Exception) -> None:
    found = find_cli_agent_execution_error(exc, category="rate_limited")
    if found is not None:
        raise found from exc
