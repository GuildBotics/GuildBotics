"""Queue-only chat refresh and the pre-publication check for chat runs."""

import os
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from guildbotics.capabilities.chat_batch import chat_batch_event_ids
from guildbotics.capabilities.task_runs import RUN_ENV, RunStore
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus, ReceiveState
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_workflow_status import is_suppressed_chat_event
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.utils.i18n_tool import t


class ChatUpdatesRequired(RuntimeError):
    """The agent must refresh and reconsider before publishing."""


def _source(person_id: str, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evidence = RunStore().evidence(run_id)
    for index in range(len(evidence) - 1, -1, -1):
        if evidence[index]["evidence_type"] == "chat_batch":
            source = evidence[index]["payload"]
            if source.get("person_id") == person_id:
                return source, evidence[index:]
            break
    raise ChatUpdatesRequired(t("cli.member.chat_updates.invalid_run"))


def _pending(source: dict[str, Any], evidence: list[dict[str, Any]]) -> list[ChatEvent]:
    store = FileConversationStateStore()
    scope = (source["service"], source["person_id"], source["channel_id"])
    delivered = set(chat_batch_event_ids(evidence))
    processed = set(store.load_channel_cursor(*scope).processed_event_ids)
    return sorted(
        (
            item.event
            for item in store.load_pending_events(*scope)
            if item.event.thread_ts == source["thread_ts"]
            and item.event.event_id not in delivered
            and item.event.author_id != source["self_user_id"]
            and not item.event.is_edit_or_delete
            and not is_suppressed_chat_event(item.event)
            and item.event.event_id not in processed
        ),
        key=lambda event: Decimal(event.message_ts),
    )


def _receive_state(source: dict[str, Any]) -> ReceiveState:
    return ChatReceiveStatus().state(
        source["service"], source["person_id"], source["channel_id"]
    )


def _receive_delay_reason(state: ReceiveState) -> str:
    if state == "catching_up":
        return t("cli.member.chat_updates.catching_up")
    return t("cli.member.chat_updates.unavailable")


def check_chat_updates(person_id: str, run_id: str) -> dict[str, Any]:
    """Deliver new input without acknowledging or removing pending events."""
    active_run = os.getenv(RUN_ENV)
    if active_run and active_run != run_id:
        raise ChatUpdatesRequired(t("cli.member.chat_updates.invalid_run"))
    source, evidence = _source(person_id, run_id)
    result: dict[str, Any] = {
        "run_id": run_id,
        "service": source["service"],
        "channel_id": source["channel_id"],
        "thread_ts": source["thread_ts"],
    }
    receive_state = _receive_state(source)
    if receive_state != "ready":
        return {
            **result,
            "status": receive_state,
            "messages": [],
            "reason": _receive_delay_reason(receive_state),
        }
    events = _pending(source, evidence)
    RunStore().append_evidence(
        run_id, "chat_updates", {"event_ids": [event.event_id for event in events]}
    )
    return {
        **result,
        "status": "new_messages" if events else "up_to_date",
        "messages": [asdict(event) for event in events],
    }


def ensure_chat_current(person_id: str) -> None:
    """Guard the source chat even when publishing to another destination."""
    run_id = os.getenv(RUN_ENV)
    if not run_id:
        return
    source, evidence = _source(person_id, run_id)
    receive_state = _receive_state(source)
    if receive_state != "ready":
        reason = _receive_delay_reason(receive_state)
    elif not any(item["evidence_type"] == "chat_updates" for item in evidence):
        reason = t("cli.member.chat_updates.not_checked")
    elif _pending(source, evidence):
        reason = t("cli.member.chat_updates.new_messages")
    else:
        return
    raise ChatUpdatesRequired(
        t(
            "cli.member.chat_updates.required",
            reason=reason,
            person=person_id,
            run_id=run_id,
        )
    )
