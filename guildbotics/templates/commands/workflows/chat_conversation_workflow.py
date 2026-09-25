from __future__ import annotations

import json
from typing import Any

from guildbotics.capabilities.chat_selection import ChatTurn
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root
from guildbotics.utils.i18n_tool import t

_IN_DISPATCH_COMPLETION_ATTEMPTS = 2


async def main(context: Any) -> None:
    """Respond to a chat batch the host selected, with an AI CLI turn.

    The host has already judged that the batch needs a response and owns the
    conversation ledger around it; this workflow asks for the turn, which the
    host drives until the member records a completion, and raises when it
    cannot.
    """
    turn = ChatTurn.model_validate(
        context.shared_state[WORKFLOW_INVOCATION_KEY].payload
    )
    person_id = context.person.person_id
    member_workspace = get_member_clone_path(person_id)
    member_workspace.mkdir(parents=True, exist_ok=True)
    prompt = turn.prompt

    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

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
        workflow_run_id=turn.run_id,
        service_name=turn.service_name,
        channel_id=turn.channel_id,
        event_id=turn.event_id,
        message_ts=turn.message_ts,
        thread_ts=turn.thread_ts,
        unprocessed_messages=_json(prompt["unprocessed_messages"]),
        participant_labels=_json(prompt["participant_labels"]),
        previous_thread_context=_json(prompt["previous_thread_context"]),
        handoff_candidates=_json(prompt["handoff_candidates"]),
        chat_participation=prompt["chat_participation"],
        language=getattr(context, "language_name", ""),
        member_workspace=str(member_workspace),
        agent_execution_context={
            "run_id": turn.run_id,
            "workspace_data_root": str(get_workspace_root()),
            "work_kind": "chat",
            "work_identity": turn.work_identity,
            "resume_policy": "auto",
            "context_cursor": turn.context_cursor,
            "event_id": turn.event_id,
            "attempt": turn.attempt,
            "rebuild_context": _json(prompt["thread_context"]),
            "rebuild_context_complete": prompt["thread_context_complete"],
            "participant_labels": _json(prompt["participant_labels"]),
            "max_completion_attempts": _IN_DISPATCH_COMPLETION_ATTEMPTS,
            # A turn that fails outright goes back to the dispatcher's
            # backoff; only a turn that ran without completing is retried.
            "retry_invoke_exceptions": False,
        },
        cwd=member_workspace,
    )
