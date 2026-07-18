from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

# Env var naming a per-dispatch file the AI CLI tool script uses to pin the agent
# conversation across retries. Antigravity writes the conversation id it created
# on the first attempt and resumes it with ``agy --conversation <id>`` on retries
# (never ``--continue``, which would resume whatever conversation last ran in the
# same workspace — possibly a different workflow). Other agents may ignore it.
CLI_AGENT_CONVERSATION_FILE_ENV = "GUILDBOTICS_CLI_AGENT_CONVERSATION_FILE"


class CompletionRetryExhausted(Exception):
    """Raised when the agent never recorded a terminal completion in the budget."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(
            f"Agent did not complete after {attempts} attempt(s): {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


async def run_with_completion_retry[T](
    *,
    invoke: Callable[[str, int], Awaitable[object]],
    check_completion: Callable[[str], T],
    max_attempts: int,
    run_id: str | None = None,
    retry_invoke_exceptions: bool = True,
) -> tuple[T, str]:
    """Run an agent turn and verify it recorded a terminal completion, retrying
    until the attempt budget is exhausted.

    This is the single retry mechanism shared by the chat and ticket workflows so
    a slow, multi-turn AI CLI tool (which may yield mid-task without finalizing) is
    driven to completion in one dispatch instead of relying on the outer
    re-dispatch / re-selection layers, which differ between the two workflows.

    A single run id is used for every attempt of one logical dispatch so partial
    evidence (e.g. a commit or reply recorded on attempt 1) accumulates under the
    same run and the final ``complete`` can land on top of it, instead of being
    stranded under a per-attempt id.

    A failed agent run (``invoke`` raises, e.g. the AI CLI tool exited non-zero) is
    treated the same as a missing completion record (``check_completion`` raises):
    it consumes an attempt and, once the budget is exhausted, surfaces as
    ``CompletionRetryExhausted`` so the caller escalates instead of letting the
    outer queue retry the agent forever.

    Args:
        invoke: ``async (run_id, attempt) -> ...`` that runs one agent turn under
            the shared run id. ``attempt`` is 1-based.
        check_completion: ``(run_id) -> status`` that returns the completion when
            the run recorded a terminal result, or raises when it did not.
        max_attempts: Upper bound on agent turns for this dispatch.

    Returns:
        ``(completion, run_id)`` for the run once it completed.

    Raises:
        CompletionRetryExhausted: when no attempt completed within the budget.
    """
    from guildbotics.capabilities.workflow_completion_events import (
        record_workflow_completed,
        record_workflow_completion_missing,
    )

    run_id = run_id or uuid4().hex
    last_error: Exception = RuntimeError("no attempts were made")
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            await invoke(run_id, attempt)
        except Exception as exc:
            if found := find_cli_agent_execution_error(exc, category="rate_limited"):
                raise found from exc
            if not retry_invoke_exceptions:
                raise
            last_error = exc
            continue
        try:
            completion = check_completion(run_id)
        except Exception as exc:
            if found := find_cli_agent_execution_error(exc, category="rate_limited"):
                raise found from exc
            # The provider turn succeeded but left no terminal completion
            # record; make that gap an explicit diagnostics event so the trace
            # is never mistaken for a completed workflow.
            record_workflow_completion_missing(
                run_id=run_id,
                attempt=attempt,
                max_attempts=attempts,
                error=str(exc),
            )
            last_error = exc
            continue
        record_workflow_completed(run_id=run_id, attempt=attempt)
        return completion, run_id
    raise CompletionRetryExhausted(attempts, last_error)


def find_cli_agent_execution_error(
    exc: BaseException, *, category: str = ""
) -> BaseException | None:
    """Find a CliAgentExecutionError through common wrapper exception chains."""
    from guildbotics.intelligences.brains.cli_agent import CliAgentExecutionError

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if isinstance(current, CliAgentExecutionError) and (
            not category or current.category == category
        ):
            return current
        last_error = getattr(current, "last_error", None)
        if isinstance(last_error, BaseException):
            stack.append(last_error)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
    return None
