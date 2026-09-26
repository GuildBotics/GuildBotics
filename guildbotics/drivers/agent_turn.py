"""Host-side completion handling for workflow agent turns."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guildbotics.capabilities.command_failures import find_cli_agent_execution_error
from guildbotics.capabilities.task_runs import RunStatus, RunStore
from guildbotics.capabilities.workflow_completion_events import (
    record_workflow_completed,
    record_workflow_completion_missing,
)
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.i18n_tool import t


class CompletionRetryExhausted(Exception):
    """Raised when the agent never recorded a terminal completion in the budget."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(
            f"Agent did not complete after {attempts} attempt(s): {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """An agent response paired with its durable completion and evidence."""

    response: Any
    completion: RunStatus
    evidence: list[dict[str, Any]]


async def run_agent_turn(
    *,
    invoke: Callable[[dict[str, Any], dict[str, str]], Awaitable[Any]],
    execution_context: Mapping[str, Any],
) -> AgentTurnResult:
    """Drive one logical workflow turn until it records completion.

    The host derives the completion record from the execution context rather
    than accepting a workflow-owned callback. This keeps both ticket and chat
    turns on the same boundary while letting each workflow choose its attempt
    budget through ``max_completion_attempts``.

    ``invoke`` receives the attempt's execution context and the prompt
    parameters the host derives from the run record for that attempt: a chat
    turn gets ``previous_attempt_evidence``, the actions its run has already
    taken, re-read before every attempt so a session that had to be recreated
    does not repeat them. These parameters replace any the workflow passed
    under the same name.

    Raises:
        CompletionRetryExhausted: When the attempt budget is exhausted. The
            caller is responsible for reporting the failure to the requester.
    """
    context = dict(execution_context)
    run_id = str(context.get("run_id") or "").strip()
    work_kind = str(context.get("work_kind") or "").strip()
    workspace_data_root = str(context.get("workspace_data_root") or "").strip()
    if not run_id:
        raise ValueError("Agent execution context requires a run_id.")
    if not workspace_data_root:
        raise ValueError("Agent execution context requires a workspace_data_root.")
    if work_kind not in {"ticket", "chat"}:
        raise ValueError(
            "Completion-managed agent turns require work_kind 'ticket' or 'chat'."
        )

    attempts = _positive_int(context.get("max_completion_attempts"), 1)
    first_attempt = _positive_int(context.get("attempt"), 1)
    retry_invoke_exceptions = bool(context.get("retry_invoke_exceptions", True))
    store = RunStore(
        get_workspace_state_path(
            "task-runs",
            workspace_root=Path(workspace_data_root),
        )
    )
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
            response = await invoke(turn_context, _turn_parameters(store, context))
        except Exception as exc:
            _raise_rate_limit(exc)
            if retry_invoke_exceptions:
                last_error = exc
                continue
            raise

        try:
            completion = store.status(run_id)
            evidence = store.evidence(run_id)
        except Exception as exc:
            _raise_rate_limit(exc)
            record_workflow_completion_missing(
                run_id=run_id,
                attempt=dispatch_attempt,
                max_attempts=attempts,
                error=str(exc),
            )
            last_error = exc
            continue

        record_workflow_completed(run_id=run_id, attempt=dispatch_attempt)
        return AgentTurnResult(
            response=response,
            completion=completion,
            evidence=evidence,
        )

    raise CompletionRetryExhausted(attempts, last_error)


def _turn_parameters(store: RunStore, context: Mapping[str, Any]) -> dict[str, str]:
    if context.get("work_kind") != "chat":
        return {}
    evidence = [
        item
        for item in store.evidence(str(context["run_id"]))
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
