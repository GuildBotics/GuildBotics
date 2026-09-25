from __future__ import annotations

import json
from typing import Any

from guildbotics.capabilities.chat_selection import ChatTurn
from guildbotics.capabilities.completion_retry import run_with_completion_retry
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root
from guildbotics.utils.i18n_tool import t

_IN_DISPATCH_COMPLETION_ATTEMPTS = 2


async def main(context: Any) -> None:
    """Respond to a chat batch the host selected, with an AI CLI turn.

    The host has already judged that the batch needs a response and owns the
    conversation ledger around it; this workflow runs the turn until it
    records a completion, and raises when it cannot.
    """
    turn = ChatTurn.model_validate(
        context.shared_state[WORKFLOW_INVOCATION_KEY].payload
    )
    person_id = context.person.person_id
    member_workspace = get_member_clone_path(person_id)
    member_workspace.mkdir(parents=True, exist_ok=True)
    store = RunStore()
    prompt = turn.prompt

    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    async def _invoke(run_id: str, attempt: int) -> None:
        await context.invoke(
            "functions/handle_chat_event",
            person_id=person_id,
            **({"effort": turn.effort} if turn.effort else {}),
            # The thread content is passed as named parameters below, so the
            # prompt must not also inherit `Context.pipe` as the user's message.
            message="",
            workflow_contract=t(
                "commands.workflows.common.workflow_contract",
                person_id=person_id,
            ),
            workflow_run_id=run_id,
            service_name=turn.service_name,
            channel_id=turn.channel_id,
            event_id=turn.event_id,
            message_ts=turn.message_ts,
            thread_ts=turn.thread_ts,
            previous_attempt_evidence=_json(
                [
                    item
                    for item in store.evidence(run_id)
                    if item["evidence_type"] != "chat_batch"
                ]
            ),
            unprocessed_messages=_json(prompt["unprocessed_messages"]),
            participant_labels=_json(prompt["participant_labels"]),
            previous_thread_context=_json(prompt["previous_thread_context"]),
            handoff_candidates=_json(prompt["handoff_candidates"]),
            chat_participation=prompt["chat_participation"],
            language=getattr(context, "language_name", ""),
            member_workspace=str(member_workspace),
            agent_execution_context={
                "run_id": run_id,
                "workspace_data_root": str(get_workspace_root()),
                "work_kind": "chat",
                "work_identity": turn.work_identity,
                "resume_policy": "auto",
                "context_cursor": turn.context_cursor,
                "event_id": turn.event_id,
                "attempt": turn.attempt + attempt - 1,
                "rebuild_context": _json(prompt["thread_context"]),
                "rebuild_context_complete": prompt["thread_context_complete"],
                "continuation_input": t(
                    "commands.workflows.common.agent_chat_continuation",
                    run_id=run_id,
                    event_id=turn.event_id,
                ),
                "participant_labels": _json(prompt["participant_labels"]),
            },
            cwd=member_workspace,
        )

    # Retry the agent in-process until it records a terminal completion. The
    # host reports a turn that still cannot complete and retries the event.
    await run_with_completion_retry(
        invoke=_invoke,
        check_completion=store.status,
        max_attempts=_IN_DISPATCH_COMPLETION_ATTEMPTS,
        run_id=turn.run_id,
        retry_invoke_exceptions=False,
    )
