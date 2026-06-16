from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from guildbotics.capabilities.task_runs import RUN_ENV, RunStore
from guildbotics.entities.message import Message
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatService,
)
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ThreadConversationState,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    get_storage_path,
    get_workspace_path,
)
from guildbotics.utils.person_profile import build_agent_profile


async def main(
    context: Any,
    chat_service: ChatService | None = None,
    state_store: ConversationStateStore | None = None,
) -> None:
    """React to one incoming chat event provided via Context.shared_state."""
    chat_service = chat_service or context.get_chat_service()
    state_store = state_store or FileConversationStateStore()
    incoming = _read_incoming_event_from_context(context)
    if incoming is not None:
        identity = await chat_service.get_bot_identity()
        await _handle_event(
            context=context,
            chat_service=chat_service,
            state_store=state_store,
            service_name=incoming.service_name,
            channel_id=incoming.channel_id,
            identity_user_id=identity.user_id,
            event=incoming.event,
        )


async def _handle_event(
    *,
    context: Any,
    chat_service: ChatService,
    state_store: ConversationStateStore,
    service_name: str,
    channel_id: str,
    identity_user_id: str,
    event: ChatEvent,
) -> None:
    person_id = context.person.person_id
    thread_state = state_store.load_thread_state(
        service_name, person_id, channel_id, event.thread_ts
    )
    channel_state = state_store.load_channel_cursor(service_name, person_id, channel_id)
    already_processed = event.event_id in set(channel_state.processed_event_ids)
    if event.is_edit_or_delete:
        if not already_processed:
            state_store.mark_processed_event(
                service_name, person_id, channel_id, event.event_id
            )
        return
    if already_processed:
        return
    if event.is_from_user(identity_user_id):
        state_store.mark_processed_event(
            service_name, person_id, channel_id, event.event_id
        )
        return
    if event.mentions and identity_user_id not in event.mentions:
        state_store.mark_processed_event(
            service_name, person_id, channel_id, event.event_id
        )
        return

    if not already_processed:
        state_store.append_thread_message(
            service_name,
            person_id,
            channel_id,
            event.thread_ts,
            ThreadMessageState(
                channel_id=event.channel_id,
                thread_ts=event.thread_ts,
                message_ts=event.message_ts,
                author_id=event.author_id,
                text=event.text,
                mentions=list(event.mentions),
                is_bot_message=event.is_bot_message,
            ),
        )

    thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    workflow_run_id = uuid4().hex
    member_workspace = _get_chat_workspace_path(context)
    if member_workspace is None:
        raise RuntimeError("Member workspace path could not be resolved.")
    prompt_payload = await _build_agent_prompt_payload(
        context=context,
        chat_service=chat_service,
        event=event,
        thread_messages=thread_messages,
        self_user_id=identity_user_id,
        thread_state=thread_state,
    )

    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("Invoker function is not set.")
    await invoke(
        "functions/handle_chat_event",
        person_id=person_id,
        workflow_run_id=workflow_run_id,
        service_name=service_name,
        channel_id=channel_id,
        event_id=event.event_id,
        message_ts=event.message_ts,
        thread_ts=event.thread_ts,
        latest_message=json.dumps(
            prompt_payload["latest_message"], ensure_ascii=False, sort_keys=True
        ),
        participant_labels=json.dumps(
            prompt_payload["participant_labels"], ensure_ascii=False, sort_keys=True
        ),
        member_profile=json.dumps(
            prompt_payload["member_profile"], ensure_ascii=False, sort_keys=True
        ),
        previous_thread_context=json.dumps(
            prompt_payload["previous_thread_context"],
            ensure_ascii=False,
            sort_keys=True,
        ),
        language=getattr(context, "language_name", ""),
        member_workspace=str(member_workspace),
        cli_agent_env={
            RUN_ENV: workflow_run_id,
            GUILDBOTICS_DATA_DIR: str(get_storage_path()),
        },
        cwd=member_workspace,
    )

    completion, evidence = _chat_run_status(workflow_run_id, member_workspace)
    if hasattr(context, "logger"):
        with suppress(Exception):
            context.logger.info(
                "chat completion=%s evidence=%s channel=%s thread=%s event=%s",
                completion.status,
                completion.evidence_types,
                channel_id,
                event.thread_ts,
                event.event_id,
            )

    state_store.mark_processed_event(
        service_name, person_id, channel_id, event.event_id
    )
    posted = _latest_chat_post_evidence(evidence)
    if posted is not None:
        payload = posted.get("payload", {})
        text = str(payload.get("text", "")).strip()
        message_ts = str(payload.get("message_ts", "")).strip()
        thread_ts = str(payload.get("thread_ts", event.thread_ts)).strip()
        if text and message_ts:
            state_store.append_thread_message(
                service_name,
                person_id,
                channel_id,
                thread_ts,
                ThreadMessageState(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    author_id=identity_user_id,
                    text=text,
                    mentions=[],
                    is_bot_message=True,
                ),
            )
    # Only record the member as a thread participant when it took a visible
    # action (reply/post/reaction). noop / blocked completions leave no Slack
    # trace, so marking the member as a participant would wrongly bias future
    # follow-up decisions toward treating the thread as one it joined.
    reacted = any(record.get("evidence_type") == "chat_reaction" for record in evidence)
    if posted is not None or reacted:
        thread_state.participants.add(person_id)
        state_store.save_thread_state(
            service_name,
            person_id,
            channel_id,
            event.thread_ts,
            thread_state,
        )


async def _build_agent_prompt_payload(
    *,
    context: Any,
    chat_service: ChatService,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    thread_state: ThreadConversationState,
) -> dict[str, Any]:
    author_labels = await _build_author_labels(
        context, self_user_id, event, thread_messages[-20:]
    )
    prompt_latest_message = _to_prompt_message_from_event(
        event, self_user_id, author_labels, chat_service
    )

    previous_thread_context = {
        "thread_topic": thread_state.thread_topic,
        "latest_focus": thread_state.latest_focus,
    }
    return {
        "latest_message": _message_to_prompt_dict(prompt_latest_message),
        "participant_labels": author_labels,
        "member_profile": build_agent_profile(getattr(context, "person", None)),
        "previous_thread_context": previous_thread_context,
    }


def _chat_run_status(
    run_id: str, member_workspace: Path
) -> tuple[Any, list[dict[str, Any]]]:
    first_error: Exception | None = None
    stores = [
        RunStore(),
        RunStore(member_workspace / ".guildbotics-data" / "task-runs"),
        RunStore(member_workspace / ".guildbotics" / "data" / "task-runs"),
    ]
    for store in stores:
        try:
            return store.status(run_id), store.evidence(run_id)
        except Exception as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
    raise RuntimeError(f"Chat run '{run_id}' was not found.")


def _latest_chat_post_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(evidence):
        if record.get("evidence_type") in {"chat_reply", "chat_post"}:
            return record
    return None


def _get_chat_workspace_path(context: Any) -> Path | None:
    person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
    if not person_id:
        return None
    path = get_workspace_path(person_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _build_author_labels(
    context: Any,
    self_user_id: str,
    event: ChatEvent | None,
    thread_messages: list[ThreadMessageState],
) -> dict[str, str]:
    ordered_ids: list[str] = []
    bot_ids: set[str] = set()
    person_labels = await _chat_user_to_person_labels(context)

    def register(user_id: str | None, *, is_bot: bool) -> None:
        if not user_id:
            return
        if user_id not in ordered_ids:
            ordered_ids.append(user_id)
        if is_bot:
            bot_ids.add(user_id)

    for message in thread_messages:
        register(message.author_id, is_bot=message.is_bot_message)
        for mention in message.mentions:
            register(mention, is_bot=False)
    if event is not None:
        register(event.author_id, is_bot=event.is_bot_message)
        for mention in event.mentions:
            register(mention, is_bot=False)

    self_person_id = str(
        getattr(getattr(context, "person", None), "person_id", "")
    ).strip()
    if not self_person_id:
        self_person_id = "self"

    labels: dict[str, str] = {}
    if self_user_id:
        labels[self_user_id] = self_person_id

    agent_index = 1
    user_index = 1
    for user_id in ordered_ids:
        if user_id in labels:
            continue
        mapped_person_id = person_labels.get(user_id)
        if mapped_person_id:
            labels[user_id] = mapped_person_id
            continue
        if user_id in bot_ids:
            labels[user_id] = f"agent_{agent_index}"
            agent_index += 1
            continue
        labels[user_id] = f"user_{user_index}"
        user_index += 1
    return labels


async def _chat_user_to_person_labels(context: Any) -> dict[str, str]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    return await _runtime_chat_user_to_person_labels(context, members)


async def _runtime_chat_user_to_person_labels(
    context: Any,
    members: list[Any],
) -> dict[str, str]:
    clone_for = getattr(context, "clone_for", None)
    if not callable(clone_for):
        return {}

    runtime_labels: dict[str, str] = {}
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if not person_id or person_id in runtime_labels.values():
            continue
        try:
            member_context = clone_for(member)
        except Exception:
            continue
        try:
            get_chat_service = getattr(member_context, "get_chat_service", None)
            if not callable(get_chat_service):
                continue
            service = get_chat_service()
            get_bot_identity = getattr(service, "get_bot_identity", None)
            if not callable(get_bot_identity):
                continue
            identity = await get_bot_identity()
            user_id = str(getattr(identity, "user_id", "")).strip()
            if user_id:
                runtime_labels[user_id] = person_id
        except Exception:
            continue
        finally:
            close = getattr(member_context, "aclose", None)
            if callable(close):
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass
    return runtime_labels


def _to_prompt_message_from_state(
    message: ThreadMessageState,
    self_user_id: str,
    author_labels: dict[str, str],
    chat_service: ChatService,
) -> Message:
    return Message(
        content=chat_service.normalize_participant_text(message.text, author_labels),
        author=_resolve_author_label(
            message.author_id, message.is_bot_message, author_labels
        ),
        author_type=_to_author_type(
            message.is_bot_message, message.author_id, self_user_id
        ),
        timestamp=message.message_ts,
    )


def _to_prompt_message_from_event(
    event: ChatEvent,
    self_user_id: str,
    author_labels: dict[str, str],
    chat_service: ChatService,
) -> Message:
    return Message(
        content=chat_service.normalize_participant_text(event.text, author_labels),
        author=_resolve_author_label(
            event.author_id, event.is_bot_message, author_labels
        ),
        author_type=_to_author_type(
            event.is_bot_message, event.author_id, self_user_id
        ),
        timestamp=event.message_ts,
    )


def _to_author_type(
    is_bot_message: bool, author_id: str | None, self_user_id: str
) -> str:
    if is_bot_message and author_id == self_user_id:
        return Message.ASSISTANT
    return Message.USER


def _resolve_author_label(
    author_id: str | None,
    is_bot_message: bool,
    author_labels: dict[str, str],
) -> str:
    if author_id:
        label = author_labels.get(author_id)
        if label:
            return label
    return "agent" if is_bot_message else "user"


def _message_to_prompt_dict(message: Message) -> dict[str, str]:
    return {
        "content": message.content,
        "author": message.author,
        "author_type": message.author_type,
    }


def _log_info(context: Any, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        logger.info(msg, *args)
    except Exception:
        return


def _read_incoming_event_from_context(context: Any) -> IncomingChatEvent | None:
    shared_state = getattr(context, "shared_state", None)
    if not isinstance(shared_state, dict):
        return None
    return IncomingChatEvent.from_shared_state(
        shared_state.get(INCOMING_CHAT_EVENT_KEY)
    )
