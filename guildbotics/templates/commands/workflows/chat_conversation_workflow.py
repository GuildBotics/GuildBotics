from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from guildbotics.commands.utils import stringify_output
from guildbotics.entities.message import Message
from guildbotics.integrations.chat_service import (
    SEMANTIC_REACTIONS,
    ChatEvent,
    ChatService,
    SemanticReaction,
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
from guildbotics.templates.commands.workflows.chat import (
    should_react as should_react_command,
)
from guildbotics.templates.commands.workflows.chat.should_react import (
    DecisionResult,
    ReactionInput,
    ReactionThreadContext,
)
from guildbotics.utils.fileio import get_workspace_path
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.memory_backend import (
    FileMemoryBackend,
    MemoryQuery,
    MemoryUpdate,
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
    thread_state = state_store.load_thread_state(
        service_name, context.person.person_id, channel_id, event.thread_ts
    )
    channel_state = state_store.load_channel_cursor(
        service_name, context.person.person_id, channel_id
    )
    already_processed = event.event_id in set(channel_state.processed_event_ids)
    if event.is_edit_or_delete:
        if not already_processed:
            state_store.mark_processed_event(
                service_name, context.person.person_id, channel_id, event.event_id
            )
        return
    if not already_processed:
        state_store.append_thread_message(
            service_name,
            context.person.person_id,
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

    decision = await _evaluate_should_react(
        context=context,
        chat_service=chat_service,
        self_person_id=context.person.person_id,
        self_user_id=identity_user_id,
        event=event,
        thread_state=thread_state,
        thread_messages=state_store.load_thread_messages(
            service_name, context.person.person_id, channel_id, event.thread_ts
        ),
        already_processed=already_processed,
    )

    if hasattr(context, "logger"):
        with suppress(Exception):
            context.logger.info(
                "chat decision=%s reason=%s channel=%s thread=%s event=%s",
                decision.decision,
                decision.reason,
                channel_id,
                event.thread_ts,
                event.event_id,
            )

    if decision.decision == "ignore":
        return

    if decision.decision == "react_only":
        if decision.reaction:
            await chat_service.add_reaction(channel_id, event.message_ts, decision.reaction)
            # Record success of side effect before subsequent state writes so replay
            # after a crash does not repeat the same reaction.
            state_store.mark_processed_event(
                service_name, context.person.person_id, channel_id, event.event_id
            )
        thread_state.participants.add(context.person.person_id)
        state_store.save_thread_state(
            service_name, context.person.person_id, channel_id, event.thread_ts, thread_state
        )
        return

    thread_messages = state_store.load_thread_messages(
        service_name, context.person.person_id, channel_id, event.thread_ts
    )
    reply_text, thread_context = await _build_reply_text(
        context, event, thread_messages, identity_user_id, chat_service, thread_state
    )
    if not reply_text.strip():
        return
    author_labels = await _build_author_labels(
        context, identity_user_id, event, thread_messages[-20:]
    )
    rendered_reply_text = chat_service.render_participant_text(reply_text, author_labels)

    post_result = await chat_service.post_message(
        channel_id, rendered_reply_text, thread_ts=event.thread_ts
    )
    # Record success of the external side effect immediately. If the process crashes
    # before source.mark_processed() runs, the event may replay but will be ignored.
    state_store.mark_processed_event(
        service_name, context.person.person_id, channel_id, event.event_id
    )
    state_store.append_thread_message(
        service_name,
        context.person.person_id,
        channel_id,
        post_result.thread_ts,
        ThreadMessageState(
            channel_id=channel_id,
            thread_ts=post_result.thread_ts,
            message_ts=post_result.message_ts,
            author_id=identity_user_id,
            text=rendered_reply_text,
            mentions=[],
            is_bot_message=True,
        ),
    )
    thread_state.participants.add(context.person.person_id)
    if thread_context.get("thread_topic"):
        thread_state.thread_topic = thread_context["thread_topic"]
    if thread_context.get("latest_focus"):
        thread_state.latest_focus = thread_context["latest_focus"]
    state_store.save_thread_state(
        service_name, context.person.person_id, channel_id, event.thread_ts, thread_state
    )
    await _update_chat_memory(
        context,
        event,
        state_store.load_thread_messages(
            service_name, context.person.person_id, channel_id, event.thread_ts
        ),
        identity_user_id,
        chat_service,
        thread_context,
        rendered_reply_text,
    )

async def _build_reply_text(
    context: Any,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    chat_service: ChatService,
    thread_state: ThreadConversationState,
) -> tuple[str, dict[str, str]]:
    text, thread_context = await _build_reply_text_via_command(
        context,
        event,
        thread_messages,
        self_user_id,
        chat_service,
        thread_state,
    )
    if not text.strip():
        # Fallback for contexts without invoker / command failures.
        text = _build_reply_text_fallback(context, event, thread_messages)
    return text, thread_context


async def _build_reply_text_via_command(
    context: Any,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    chat_service: ChatService,
    thread_state: ThreadConversationState,
) -> tuple[str, dict[str, str]]:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        return "", {}

    author_labels = await _build_author_labels(
        context, self_user_id, event, thread_messages[-20:]
    )
    prompt_thread_messages = [
        _to_prompt_message_from_state(message, self_user_id, author_labels, chat_service)
        for message in thread_messages[-20:]
    ]
    prompt_latest_message = _to_prompt_message_from_event(
        event, self_user_id, author_labels, chat_service
    )

    payload = {
        "latest_message": _message_to_prompt_dict(prompt_latest_message),
        "thread_messages": [
            _message_to_prompt_dict(message) for message in prompt_thread_messages
        ],
        "agent_profile": build_agent_profile(getattr(context, "person", None)),
        "previous_thread_context": {
            "thread_topic": thread_state.thread_topic,
            "latest_focus": thread_state.latest_focus,
        },
    }

    transcript_lines = [
        f"[{message.author}] {message.content}" for message in prompt_thread_messages
    ]
    if not transcript_lines:
        transcript_lines.append(
            f"[{prompt_latest_message.author}] {prompt_latest_message.content}"
        )
    transcript = "\n".join(transcript_lines)

    old_pipe = getattr(context, "pipe", "")
    has_shared_state = isinstance(getattr(context, "shared_state", None), dict)
    old_shared_state = deepcopy(context.shared_state) if has_shared_state else None
    try:
        memory_backend = _get_memory_backend(context)
        workspace_path = _get_chat_workspace_path(context)
        if has_shared_state:
            context.shared_state["chat_reply_input"] = payload
        if hasattr(context, "pipe"):
            context.pipe = transcript
        thread_context = await _classify_thread_context(context)
        if has_shared_state:
            context.shared_state["chat_reply_input"]["thread_context"] = thread_context
        memory_context = (
            memory_backend.recall(_memory_query(context, thread_context, transcript))
            if memory_backend is not None
            else None
        )
        if has_shared_state:
            context.shared_state["chat_reply_input"]["memory_context"] = (
                asdict(memory_context) if memory_context is not None else {}
            )
        reply_intent = await _classify_reply_intent(context)
        if has_shared_state:
            context.shared_state["chat_reply_input"]["reply_intent"] = reply_intent
        invoke_kwargs = {"cwd": workspace_path} if workspace_path is not None else {}
        reply_result = await invoke(
            "workflows/chat/chat_reply_actionable", **invoke_kwargs
        )
        reply_text = stringify_output(reply_result).strip()
        return reply_text, thread_context
    except Exception:
        _log_info(
            context,
            "chat reply command failed, falling back to placeholder reply generation",
        )
        return "", {}
    finally:
        if has_shared_state and old_shared_state is not None:
            context.shared_state.clear()
            context.shared_state.update(old_shared_state)
        if hasattr(context, "pipe"):
            context.pipe = old_pipe


def _build_reply_text_fallback(
    context: Any, event: ChatEvent, thread_messages: list[ThreadMessageState]
) -> str:
    return t("commands.workflows.chat_conversation_workflow.reply_generation_failed")


def _get_memory_backend(context: Any) -> FileMemoryBackend | None:
    person = getattr(context, "person", None)
    team = getattr(context, "team", None)
    if person is None or team is None or getattr(team, "project", None) is None:
        return None
    try:
        return FileMemoryBackend(person, team)
    except Exception:
        return None


def _get_chat_workspace_path(context: Any) -> Path | None:
    person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
    if not person_id:
        return None
    path = get_workspace_path(person_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _update_chat_memory(
    context: Any,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    chat_service: ChatService,
    thread_context: dict[str, str],
    reply_text: str,
) -> None:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke) or not reply_text.strip():
        return
    memory_backend = _get_memory_backend(context)
    if memory_backend is None:
        return
    try:
        author_labels = await _build_author_labels(
            context, self_user_id, event, thread_messages[-20:]
        )
        messages = [
            _message_to_prompt_dict(
                _to_prompt_message_from_state(
                    message, self_user_id, author_labels, chat_service
                )
            )
            for message in thread_messages[-20:]
        ]
        transcript = "\n".join(
            f"[{message['author']}] {message['content']}" for message in messages
        )
        memory_context = memory_backend.recall(
            _memory_query(context, thread_context, transcript)
        )
        payload = {
            "agent_profile": build_agent_profile(getattr(context, "person", None)),
            "thread_context": thread_context,
            "thread_messages": messages,
            "memory_context": asdict(memory_context),
            "reply_text": reply_text,
        }
        old_pipe = getattr(context, "pipe", "")
        has_shared_state = isinstance(getattr(context, "shared_state", None), dict)
        old_shared_state = deepcopy(context.shared_state) if has_shared_state else None
        try:
            if has_shared_state:
                context.shared_state["chat_memory_update_input"] = payload
            if hasattr(context, "pipe"):
                context.pipe = transcript
            result = await invoke("workflows/chat/chat_memory_update")
        finally:
            if has_shared_state and old_shared_state is not None:
                context.shared_state.clear()
                context.shared_state.update(old_shared_state)
            if hasattr(context, "pipe"):
                context.pipe = old_pipe
        memory_backend.remember(_normalize_memory_update(result))
    except Exception:
        _log_info(context, "chat memory update failed")


def _memory_query(context: Any, thread_context: dict[str, str], transcript: str) -> MemoryQuery:
    person = getattr(context, "person", None)
    return MemoryQuery(
        person_id=str(getattr(person, "person_id", "")).strip(),
        thread_topic=str(thread_context.get("thread_topic", "")).strip(),
        latest_focus=str(thread_context.get("latest_focus", "")).strip(),
        transcript=transcript,
    )


def _normalize_memory_update(result: Any) -> MemoryUpdate:
    if isinstance(result, dict):
        get = result.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(result, key, default)

    return MemoryUpdate(
        should_update=bool(get("should_update", False)),
        topic_id=str(get("topic_id", "")).strip(),
        title=str(get("title", "")).strip(),
        summary=str(get("summary", "")).strip(),
        memory=str(get("memory", "")).strip(),
    )


async def _classify_reply_intent(context: Any) -> dict[str, str]:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        return {
            "label": "answer",
            "reason": "default_without_invoker",
            "confidence": "0.0",
        }
    try:
        result = await invoke("workflows/chat/chat_reply_intent")
    except Exception:
        _log_info(context, "chat reply intent classification failed, defaulting to answer")
        return {
            "label": "answer",
            "reason": "intent_classification_failed",
            "confidence": "0.0",
        }
    return _normalize_reply_intent(result)


async def _classify_thread_context(context: Any) -> dict[str, str]:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        return {}
    try:
        result = await invoke("workflows/chat/chat_thread_context")
    except Exception:
        _log_info(context, "chat thread context classification failed, defaulting to empty context")
        return {}
    return _normalize_thread_context(result)


def _normalize_reply_intent(result: Any) -> dict[str, str]:
    if isinstance(result, dict):
        label = str(result.get("label", "")).strip()
        reason = str(result.get("reason", "")).strip()
        confidence = str(result.get("confidence", "")).strip()
    else:
        label = str(getattr(result, "label", "")).strip()
        reason = str(getattr(result, "reason", "")).strip()
        confidence = str(getattr(result, "confidence", "")).strip()

    if label not in {"answer", "supplement", "challenge", "clarify", "summarize"}:
        label = "answer"
    return {
        "label": label,
        "reason": reason or "no_reason",
        "confidence": confidence or "0.0",
    }


def _normalize_thread_context(result: Any) -> dict[str, str]:
    if isinstance(result, dict):
        thread_topic = str(result.get("thread_topic", "")).strip()
        latest_focus = str(result.get("latest_focus", "")).strip()
        reason = str(result.get("reason", "")).strip()
        confidence = str(result.get("confidence", "")).strip()
    else:
        thread_topic = str(getattr(result, "thread_topic", "")).strip()
        latest_focus = str(getattr(result, "latest_focus", "")).strip()
        reason = str(getattr(result, "reason", "")).strip()
        confidence = str(getattr(result, "confidence", "")).strip()
    normalized = {
        "thread_topic": thread_topic,
        "latest_focus": latest_focus,
        "reason": reason or "no_reason",
        "confidence": confidence or "0.0",
    }
    if not normalized["thread_topic"] and not normalized["latest_focus"]:
        return {}
    return normalized


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

    self_person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
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
        author=_resolve_author_label(message.author_id, message.is_bot_message, author_labels),
        author_type=_to_author_type(message.is_bot_message, message.author_id, self_user_id),
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
        author=_resolve_author_label(event.author_id, event.is_bot_message, author_labels),
        author_type=_to_author_type(event.is_bot_message, event.author_id, self_user_id),
        timestamp=event.message_ts,
    )


def _to_author_type(is_bot_message: bool, author_id: str | None, self_user_id: str) -> str:
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


async def _evaluate_should_react(
    *,
    context: Any,
    chat_service: ChatService,
    self_person_id: str,
    self_user_id: str,
    event: ChatEvent,
    thread_state: ThreadConversationState,
    thread_messages: list[ThreadMessageState],
    already_processed: bool,
) -> DecisionResult:
    if already_processed:
        return DecisionResult(decision="ignore", reason="already_processed")

    author_labels = await _build_author_labels(context, self_user_id, event, thread_messages[-20:])
    reaction_input = ReactionInput(
        self_person_id=self_person_id,
        self_user_id=self_user_id,
        event=event,
        thread_context=ReactionThreadContext(
            participants=set(thread_state.participants),
        ),
        thread_messages=[
            _message_to_prompt_dict(
                _to_prompt_message_from_state(
                    message, self_user_id, author_labels, chat_service
                )
            )
            for message in thread_messages[-20:]
        ],
        already_processed=already_processed,
    )

    invoke = getattr(context, "invoke", None)
    if callable(invoke):
        result = await invoke(
            "workflows/chat/should_react",
            channel_type="chat",
            reaction_input=reaction_input,
        )
    else:
        result = await should_react_command.main(
            context,
            channel_type="chat",
            reaction_input=reaction_input,
        )
    normalized = _normalize_policy_decision(result)
    if normalized is None:
        raise RuntimeError("should_react returned an invalid decision payload.")
    return normalized


def _normalize_policy_decision(result: Any) -> DecisionResult | None:
    if isinstance(result, dict):
        decision = str(result.get("decision", "")).strip()
        reason = str(result.get("reason", "")).strip()
        reaction = result.get("reaction")
    else:
        decision = str(getattr(result, "decision", "")).strip()
        reason = str(getattr(result, "reason", "")).strip()
        reaction = getattr(result, "reaction", None)

    if decision not in {"ignore", "react_only", "reply"}:
        return None
    normalized_reaction: SemanticReaction | None = None
    if reaction is not None:
        reaction_value = str(reaction).strip()
        if reaction_value not in SEMANTIC_REACTIONS:
            return None
        normalized_reaction = cast(SemanticReaction, reaction_value)
    return DecisionResult(
        decision=decision,
        reason=reason or "no_reason",
        reaction=normalized_reaction,
    )


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
    return IncomingChatEvent.from_shared_state(shared_state.get(INCOMING_CHAT_EVENT_KEY))
