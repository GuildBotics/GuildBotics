from __future__ import annotations

import json
import re
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
    ThreadHandoffState,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    get_workspace_data_root,
)

CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
_SLACK_MENTION_RE = re.compile(r"<@([^>|]+)(?:\|[^>]+)?>")
_MAX_HANDOFF_TEXT_LENGTH = 240


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

    thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    latest_mentions_self = identity_user_id in set(event.mentions)
    thread_has_mentioned_self = _thread_has_mentioned_user(
        thread_messages, identity_user_id
    )
    if event.mentions and not latest_mentions_self:
        state_store.mark_processed_event(
            service_name, person_id, channel_id, event.event_id
        )
        return
    if not latest_mentions_self and not thread_has_mentioned_self:
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
    workspace_data_root = get_workspace_data_root()
    member_workspace = _get_chat_workspace_path(context, workspace_data_root)
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
        previous_thread_context=json.dumps(
            prompt_payload["previous_thread_context"],
            ensure_ascii=False,
            sort_keys=True,
        ),
        handoff_candidates=json.dumps(
            prompt_payload["handoff_candidates"], ensure_ascii=False, sort_keys=True
        ),
        language=getattr(context, "language_name", ""),
        member_workspace=str(member_workspace),
        cli_agent_env={
            RUN_ENV: workflow_run_id,
            GUILDBOTICS_DATA_DIR: str(workspace_data_root),
            CHAT_PARTICIPANT_LABELS_ENV: json.dumps(
                prompt_payload["participant_labels"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
        cwd=member_workspace,
    )

    completion, evidence = _chat_run_status(
        workflow_run_id, workspace_data_root / "task-runs", member_workspace
    )
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
    mentioned_user_ids: list[str] = []
    if posted is not None:
        payload = posted.get("payload", {})
        text = str(payload.get("text", "")).strip()
        message_ts = str(payload.get("message_ts", "")).strip()
        thread_ts = str(payload.get("thread_ts", event.thread_ts)).strip()
        mentioned_user_ids = _mentioned_user_ids_from_text(text)
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
                    mentions=mentioned_user_ids,
                    is_bot_message=True,
                ),
            )
            _record_handoffs(
                context=context,
                thread_state=thread_state,
                participant_labels=prompt_payload["participant_labels"],
                mentioned_user_ids=mentioned_user_ids,
                source_person_id=person_id,
                message_ts=message_ts,
                text=text,
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
    person_labels = await _chat_user_to_person_labels(context)
    author_labels = _build_author_labels(
        context, self_user_id, event, thread_messages[-20:], person_labels
    )
    prompt_latest_message = _to_prompt_message_from_event(
        event, self_user_id, author_labels, chat_service
    )

    previous_thread_context = {
        "thread_topic": thread_state.thread_topic,
        "latest_focus": thread_state.latest_focus,
        "handoffs": [_handoff_to_prompt_dict(item) for item in thread_state.handoffs],
    }
    return {
        "latest_message": _message_to_prompt_dict(prompt_latest_message),
        "participant_labels": author_labels,
        "handoff_candidates": _build_handoff_candidates(context, person_labels),
        "previous_thread_context": previous_thread_context,
    }


def _chat_run_status(
    run_id: str, task_run_root: Path, member_workspace: Path
) -> tuple[Any, list[dict[str, Any]]]:
    first_error: Exception | None = None
    stores = [
        RunStore(task_run_root),
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


def _mentioned_user_ids_from_text(text: str) -> list[str]:
    return list(
        dict.fromkeys(match.group(1) for match in _SLACK_MENTION_RE.finditer(text))
    )


def _record_handoffs(
    *,
    context: Any,
    thread_state: ThreadConversationState,
    participant_labels: dict[str, str],
    mentioned_user_ids: list[str],
    source_person_id: str,
    message_ts: str,
    text: str,
) -> None:
    if not mentioned_user_ids:
        return
    roles_by_person = _roles_by_person(context)
    existing = {
        (handoff.person_id, handoff.message_ts) for handoff in thread_state.handoffs
    }
    for user_id in mentioned_user_ids:
        person_id = participant_labels.get(user_id, "")
        if not person_id or person_id == source_person_id:
            continue
        key = (person_id, message_ts)
        if key in existing:
            continue
        thread_state.handoffs.append(
            ThreadHandoffState(
                person_id=person_id,
                roles=roles_by_person.get(person_id, []),
                message_ts=message_ts,
                text=_truncate_handoff_text(text),
                thread_topic=thread_state.thread_topic,
                latest_focus=thread_state.latest_focus,
            )
        )
        existing.add(key)


def _roles_by_person(context: Any) -> dict[str, list[str]]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    roles: dict[str, list[str]] = {}
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if not person_id:
            continue
        member_roles = getattr(member, "roles", {}) or {}
        roles[person_id] = [str(role_id) for role_id in member_roles]
    return roles


def _truncate_handoff_text(text: str) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= _MAX_HANDOFF_TEXT_LENGTH:
        return normalized
    return normalized[: _MAX_HANDOFF_TEXT_LENGTH - 1].rstrip() + "..."


def _handoff_to_prompt_dict(handoff: ThreadHandoffState) -> dict[str, Any]:
    return {
        "person_id": handoff.person_id,
        "roles": handoff.roles,
        "message_ts": handoff.message_ts,
        "text": handoff.text,
        "thread_topic": handoff.thread_topic,
        "latest_focus": handoff.latest_focus,
    }


def _thread_has_mentioned_user(
    thread_messages: list[ThreadMessageState], user_id: str
) -> bool:
    if not user_id:
        return False
    return any(user_id in set(message.mentions) for message in thread_messages)


def _get_chat_workspace_path(context: Any, workspace_data_root: Path) -> Path | None:
    person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
    if not person_id:
        return None
    path = workspace_data_root / "workspaces" / person_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_author_labels(
    context: Any,
    self_user_id: str,
    event: ChatEvent | None,
    thread_messages: list[ThreadMessageState],
    person_labels: dict[str, str],
) -> dict[str, str]:
    ordered_ids: list[str] = []
    bot_ids: set[str] = set()

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
    for user_id, person_id in person_labels.items():
        if user_id not in labels:
            labels[user_id] = person_id
    return labels


def _build_handoff_candidates(
    context: Any, person_labels: dict[str, str]
) -> list[dict[str, Any]]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    self_person_id = str(
        getattr(getattr(context, "person", None), "person_id", "")
    ).strip()
    mentionable_person_ids = set(person_labels.values())
    candidates: list[dict[str, Any]] = []
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if (
            not person_id
            or person_id == self_person_id
            or person_id not in mentionable_person_ids
        ):
            continue
        roles = _handoff_roles(member)
        if not roles:
            continue
        candidates.append(
            {
                "person_id": person_id,
                "name": str(getattr(member, "name", "")).strip(),
                "mention": f"@{person_id}",
                "roles": roles,
            }
        )
    return candidates


def _handoff_roles(member: Any) -> dict[str, dict[str, str]]:
    raw_roles = getattr(member, "roles", {}) or {}
    if not isinstance(raw_roles, dict):
        return {}

    roles: dict[str, dict[str, str]] = {}
    for fallback_id, role in raw_roles.items():
        role_id = str(getattr(role, "id", fallback_id)).strip()
        if not role_id:
            continue
        role_info = {
            key: value
            for key, value in {
                "summary": str(getattr(role, "summary", "")).strip(),
                "description": str(getattr(role, "description", "")).strip(),
            }.items()
            if value
        }
        roles[role_id] = role_info
    return roles


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
        slack_user_id = str(
            (getattr(member, "account_info", {}) or {}).get("slack_user_id", "")
        ).strip()
        if slack_user_id:
            runtime_labels[slack_user_id] = person_id
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
