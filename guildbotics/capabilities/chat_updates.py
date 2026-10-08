"""Queue-only chat refresh and the pre-publication check for chat runs.

Both act on the chat run of the current member invocation: its source thread
is the run's work as the host settled it, never what the run recorded.
"""

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from guildbotics.capabilities.chat_batch import chat_batch_event_ids
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus, ReceiveState
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_workflow_status import is_suppressed_chat_event
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.member_invocation import (
    ChatSubject,
    current_member_invocation,
)
from guildbotics.utils.i18n_tool import t


class ChatUpdatesRequired(RuntimeError):
    """The agent must refresh and reconsider before publishing."""


def current_chat_run() -> tuple[str, ChatSubject] | None:
    """The current invocation's chat run and the event it answers, if any."""
    invocation = current_member_invocation()
    work = invocation.work
    if not invocation.run_id or work is None or work.chat is None:
        return None
    return invocation.run_id, work.chat


def _since_batch(run_id: str) -> list[dict[str, Any]]:
    """The run's evidence from the batch it was last delivered."""
    evidence = RunStore().evidence(run_id)
    for index in range(len(evidence) - 1, -1, -1):
        if evidence[index]["evidence_type"] == "chat_batch":
            return evidence[index:]
    raise ChatUpdatesRequired(t("cli.member.chat_updates.invalid_run"))


def _pending(
    person_id: str, subject: ChatSubject, evidence: list[dict[str, Any]]
) -> list[ChatEvent]:
    store = FileConversationStateStore()
    scope = (subject.service, person_id, subject.channel_id)
    delivered = set(chat_batch_event_ids(evidence))
    processed = set(store.load_channel_cursor(*scope).processed_event_ids)
    return sorted(
        (
            item.event
            for item in store.load_pending_events(*scope)
            if item.event.thread_ts == subject.thread_ts
            and item.event.event_id not in delivered
            and item.event.author_id != subject.self_user_id
            and not item.event.is_edit_or_delete
            and not is_suppressed_chat_event(item.event)
            and item.event.event_id not in processed
        ),
        key=lambda event: Decimal(event.message_ts),
    )


def _receive_state(person_id: str, subject: ChatSubject) -> ReceiveState:
    return ChatReceiveStatus().state(subject.service, person_id, subject.channel_id)


def _receive_delay_reason(state: ReceiveState) -> str:
    if state == "catching_up":
        return t("cli.member.chat_updates.catching_up")
    return t("cli.member.chat_updates.unavailable")


def check_chat_updates(person_id: str) -> dict[str, Any]:
    """Deliver new input without acknowledging or removing pending events.

    Raises:
        ChatUpdatesRequired: Outside a chat run.
    """
    chat_run = current_chat_run()
    if chat_run is None:
        raise ChatUpdatesRequired(t("cli.member.chat_updates.invalid_run"))
    run_id, subject = chat_run
    evidence = _since_batch(run_id)
    result: dict[str, Any] = {
        "run_id": run_id,
        "service": subject.service,
        "channel_id": subject.channel_id,
        "thread_ts": subject.thread_ts,
    }
    receive_state = _receive_state(person_id, subject)
    if receive_state != "ready":
        return {
            **result,
            "status": receive_state,
            "messages": [],
            "reason": _receive_delay_reason(receive_state),
        }
    events = _pending(person_id, subject, evidence)
    RunStore().append_evidence(
        run_id, "chat_updates", {"event_ids": [event.event_id for event in events]}
    )
    return {
        **result,
        "status": "new_messages" if events else "up_to_date",
        "messages": [asdict(event) for event in events],
    }


def noop_payload(subject: ChatSubject, reason: str) -> dict[str, Any]:
    """The evidence of a chat run deciding that ``subject`` needs no action."""
    return {
        "service": subject.service,
        "channel_id": subject.channel_id,
        "thread_ts": subject.thread_ts,
        "event_id": subject.event_id,
        "reason": reason,
        "noop": True,
    }


def ensure_chat_current(person_id: str) -> None:
    """Guard the source chat of a chat run, even when publishing to another
    destination; anything else publishes unguarded."""
    chat_run = current_chat_run()
    if chat_run is None:
        return
    run_id, subject = chat_run
    evidence = _since_batch(run_id)
    receive_state = _receive_state(person_id, subject)
    if receive_state != "ready":
        reason = _receive_delay_reason(receive_state)
    elif not any(item["evidence_type"] == "chat_updates" for item in evidence):
        reason = t("cli.member.chat_updates.not_checked")
    elif _pending(person_id, subject, evidence):
        reason = t("cli.member.chat_updates.new_messages")
    else:
        return
    raise ChatUpdatesRequired(
        t(
            "cli.member.chat_updates.required",
            reason=reason,
            person=person_id,
        )
    )
