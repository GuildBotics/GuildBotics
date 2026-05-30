from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
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
from guildbotics.intelligences.functions import should_keep_chat_memory_update
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
from guildbotics.utils.cognee_memory_backend import (
    CogneeMemoryBackend,
    FakeMemoryBackend,
)
from guildbotics.utils.fileio import get_workspace_path
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.memory_backend import (
    FileMemoryBackend,
    MemoryBackend,
    MemoryContext,
    MemoryForgetRequest,
    MemoryItem,
    MemoryQuery,
    MemoryUpdate,
    write_memory_context_trace,
    write_memory_forget_trace,
    write_memory_recall_trace,
    write_memory_remember_decision_trace,
    write_memory_remember_trace,
)
from guildbotics.utils.person_profile import build_agent_profile

_MIN_CHAT_TIMESTAMP = 946684800.0
_TOPIC_COALESCE_SIMILARITY_THRESHOLD = 0.3
_TOPIC_STEM_TOKEN_COUNT = 2


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
            await chat_service.add_reaction(
                channel_id, event.message_ts, decision.reaction
            )
            # Record success of side effect before subsequent state writes so replay
            # after a crash does not repeat the same reaction.
            state_store.mark_processed_event(
                service_name, context.person.person_id, channel_id, event.event_id
            )
        thread_messages = state_store.load_thread_messages(
            service_name, context.person.person_id, channel_id, event.thread_ts
        )
        reply_text, thread_context = await _build_reply_text(
            context,
            event,
            thread_messages,
            identity_user_id,
            chat_service,
            thread_state,
        )
        thread_state.participants.add(context.person.person_id)
        if thread_context.get("thread_topic"):
            thread_state.thread_topic = thread_context["thread_topic"]
        if thread_context.get("latest_focus"):
            thread_state.latest_focus = thread_context["latest_focus"]
        state_store.save_thread_state(
            service_name,
            context.person.person_id,
            channel_id,
            event.thread_ts,
            thread_state,
        )
        await _update_chat_memory(
            context,
            event,
            thread_messages,
            identity_user_id,
            chat_service,
            thread_context,
            reply_text,
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
    rendered_reply_text = chat_service.render_participant_text(
        reply_text, author_labels
    )

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
        service_name,
        context.person.person_id,
        channel_id,
        event.thread_ts,
        thread_state,
    )
    await _update_chat_memory(
        context,
        event,
        thread_messages,
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
        _to_prompt_message_from_state(
            message, self_user_id, author_labels, chat_service
        )
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
        memory_query = _memory_query(
            context,
            thread_context,
            transcript,
            event,
            consumer="reply_generation",
        )
        memory_context = _recall_memory_context(context, memory_backend, memory_query)
        if memory_context is not None:
            write_memory_recall_trace(memory_context)
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
        if memory_context is not None:
            write_memory_context_trace(
                event="memory.context.prompted",
                backend=memory_context.backend,
                person_id=memory_context.person_id,
                consumer="reply_generation",
                query=memory_context.query,
                items=memory_context.items,
                extra={
                    "thread_context": thread_context,
                    "reply_intent": reply_intent,
                    "latest_message_excerpt": prompt_latest_message.content[:500],
                    "reply_text_excerpt": reply_text[:500],
                    "note": (
                        "These memory items were supplied to reply generation; "
                        "this event does not assert they influenced the final reply."
                    ),
                },
            )
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


def _get_memory_backend(context: Any) -> MemoryBackend | None:
    person = getattr(context, "person", None)
    if person is None:
        return None
    person_id = str(getattr(person, "person_id", "")).strip()
    backend_name = os.getenv("GUILDBOTICS_MEMORY_BACKEND", "cognee").strip().lower()
    try:
        if backend_name == "file":
            team = getattr(context, "team", None)
            if team is None or getattr(team, "project", None) is None:
                return None
            return FileMemoryBackend(person, team)
        if backend_name == "fake":
            return FakeMemoryBackend(person_id)
        if backend_name in {"", "cognee"}:
            return CogneeMemoryBackend(person_id)
    except Exception as exc:
        _log_info(context, f"memory backend unavailable: {backend_name}: {exc}")
        return None
    _log_info(context, f"unknown memory backend: {backend_name}")
    return None


def _recall_memory_context(
    context: Any,
    memory_backend: MemoryBackend | None,
    query: MemoryQuery,
) -> MemoryContext | None:
    if memory_backend is None:
        return None
    try:
        return memory_backend.recall(query)
    except Exception as exc:
        _log_info(context, f"memory recall failed: {exc}")
        return MemoryContext(
            backend=_memory_backend_name(memory_backend),
            person_id=query.person_id,
            query=query.trace_payload(),
            status="failed",
            error={
                "type": exc.__class__.__name__,
                "message": str(exc),
            },
        )


def _memory_backend_name(memory_backend: MemoryBackend | None) -> str:
    if memory_backend is None:
        return ""
    backend_name = memory_backend.__class__.__name__.removesuffix("MemoryBackend")
    return backend_name.lower() or memory_backend.__class__.__name__.lower()


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
    memory_debug: dict[str, Any] = {
        "person_id": str(
            getattr(getattr(context, "person", None), "person_id", "")
        ).strip(),
        "backend": _memory_backend_name(memory_backend),
        "event_id": str(getattr(event, "event_id", "")).strip(),
        "thread_ts": str(getattr(event, "thread_ts", "")).strip(),
        "channel": str(getattr(event, "channel_id", "")).strip(),
    }
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
        memory_query = _memory_query(
            context,
            thread_context,
            transcript,
            event,
            consumer="memory_update",
        )
        memory_context = _recall_memory_context(context, memory_backend, memory_query)
        if memory_context is None:
            memory_context = MemoryContext(
                backend=_memory_backend_name(memory_backend),
                person_id=memory_query.person_id,
                query=memory_query.trace_payload(),
            )
        write_memory_recall_trace(memory_context)
        write_memory_context_trace(
            event="memory.context.prompted",
            backend=memory_context.backend,
            person_id=memory_context.person_id,
            consumer="memory_update",
            query=memory_context.query,
            items=memory_context.items,
            extra={
                "thread_context": thread_context,
                "reply_text_excerpt": reply_text[:500],
                "note": (
                    "These memory items were supplied to memory-update judgement; "
                    "this event does not assert they were persisted or used."
                ),
            },
        )
        payload = {
            "agent_profile": build_agent_profile(getattr(context, "person", None)),
            "thread_context": thread_context,
            "event_time": _chat_event_time_payload(event),
            "current_time": _current_time_payload(),
            "thread_messages": messages,
            "memory_context": asdict(memory_context),
            "reply_text": reply_text,
        }
        memory_debug["thread_context"] = thread_context
        memory_debug["event_time"] = payload["event_time"]
        memory_debug["current_time"] = payload["current_time"]
        old_pipe = getattr(context, "pipe", "")
        has_shared_state = isinstance(getattr(context, "shared_state", None), dict)
        old_shared_state = deepcopy(context.shared_state) if has_shared_state else None
        try:
            if has_shared_state:
                context.shared_state["chat_memory_update_input"] = payload
            if hasattr(context, "pipe"):
                context.pipe = transcript
            result = await invoke("workflows/chat/chat_memory_update")
            memory_debug["proposal"] = _memory_update_proposal_payload(result)
        finally:
            if has_shared_state and old_shared_state is not None:
                context.shared_state.clear()
                context.shared_state.update(old_shared_state)
            if hasattr(context, "pipe"):
                context.pipe = old_pipe
        memory_update = _normalize_memory_update(result, context=context, event=event)
        memory_debug["normalized_update"] = asdict(memory_update)
        memory_update, gate_trace = await _gate_memory_update_decision(
            context,
            update=memory_update,
            proposal=result,
            payload=payload,
        )
        memory_update = _coalesce_memory_topic(memory_update, memory_context)
        memory_debug["gate"] = gate_trace
        memory_update = _with_default_memory_retention(memory_update, event=event)
        memory_update = _ensure_temporary_topic_id(
            memory_update,
            memory_context,
            event=event,
        )
        memory_debug["normalized_update_after_kind"] = asdict(memory_update)
        transition_update = _memory_transition_update(
            memory_update=memory_update,
            memory_context=memory_context,
            context=context,
            event=event,
        )
        memory_debug["transition_update"] = (
            asdict(transition_update) if transition_update is not None else None
        )
        for forget_request in _memory_forget_requests(
            result,
            memory_update=memory_update,
            memory_context=memory_context,
            context=context,
            event=event,
        ):
            write_memory_forget_trace(memory_backend.forget(forget_request))
        transition_result = None
        if transition_update is not None:
            transition_result = memory_backend.remember(transition_update)
            write_memory_remember_trace(transition_result)
        write_result = memory_backend.remember(memory_update)
        write_memory_remember_decision_trace(
            {
                "backend": write_result.backend,
                "person_id": write_result.person_id,
                "status": write_result.status,
                "error": write_result.error,
                "source": write_result.source,
                "input": _memory_decision_trace_input(payload),
                "proposal": _memory_update_proposal_payload(result),
                "gate": gate_trace,
                "final": {
                    "should_update": memory_update.should_update,
                    "transition_item_id": (
                        transition_result.item_id
                        if transition_result is not None
                        else ""
                    ),
                    "transition_changed": (
                        transition_result.changed
                        if transition_result is not None
                        else False
                    ),
                    "changed": write_result.changed,
                    "reference": write_result.reference,
                    "item_id": write_result.item_id,
                    "title": write_result.title,
                    "metadata": write_result.metadata,
                    "retention": memory_update.retention,
                },
            }
        )
        write_memory_remember_trace(write_result)
    except Exception as exc:
        write_memory_remember_decision_trace(
            {
                "event": "memory.update.error",
                "backend": memory_debug.get("backend", ""),
                "person_id": memory_debug.get("person_id", ""),
                "status": "failed",
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                },
                "debug": memory_debug,
            }
        )
        _log_info(
            context, f"chat memory update failed: {exc.__class__.__name__}: {exc}"
        )


def _memory_query(
    context: Any,
    thread_context: dict[str, str],
    transcript: str,
    event: ChatEvent | None = None,
    *,
    consumer: str = "",
) -> MemoryQuery:
    person = getattr(context, "person", None)
    person_id = str(getattr(person, "person_id", "")).strip()
    return MemoryQuery(
        person_id=person_id,
        thread_topic=str(thread_context.get("thread_topic", "")).strip(),
        latest_focus=str(thread_context.get("latest_focus", "")).strip(),
        transcript=transcript,
        source=_memory_source(event),
        scope={"person_id": person_id} if person_id else {},
        metadata={"consumer": consumer} if consumer else {},
    )


def _normalize_memory_update(
    result: Any,
    *,
    context: Any | None = None,
    event: ChatEvent | None = None,
) -> MemoryUpdate:
    if isinstance(result, dict):
        get = result.get
    else:

        def get(key: str, default: Any = None) -> Any:
            return getattr(result, key, default)

    person = getattr(context, "person", None) if context is not None else None
    person_id = str(getattr(person, "person_id", "")).strip()
    return MemoryUpdate(
        should_update=bool(get("should_update", False)),
        topic_id=str(get("topic_id", "")).strip(),
        title=str(get("title", "")).strip(),
        summary=str(get("summary", "")).strip(),
        memory=str(get("memory", "")).strip(),
        source=_memory_source(event),
        scope={"person_id": person_id} if person_id else {},
        metadata=_memory_update_metadata(result),
        retention=_memory_update_retention(result),
    )


async def _gate_memory_update(
    context: Any,
    *,
    update: MemoryUpdate,
    proposal: Any,
    payload: dict[str, Any],
) -> MemoryUpdate:
    gated_update, _ = await _gate_memory_update_decision(
        context,
        update=update,
        proposal=proposal,
        payload=payload,
    )
    return gated_update


async def _gate_memory_update_decision(
    context: Any,
    *,
    update: MemoryUpdate,
    proposal: Any,
    payload: dict[str, Any],
) -> tuple[MemoryUpdate, dict[str, Any]]:
    try:
        decision = await should_keep_chat_memory_update(
            context,
            {
                "proposal": _memory_update_proposal_payload(proposal),
                "normalized_update": asdict(update),
                "agent_profile": payload.get("agent_profile", {}),
                "thread_context": payload.get("thread_context", {}),
                "event_time": payload.get("event_time", {}),
                "current_time": payload.get("current_time", {}),
                "memory_context": payload.get("memory_context", {}),
                "thread_messages": payload.get("thread_messages", []),
                "reply_text": payload.get("reply_text", ""),
            },
        )
    except Exception:
        _log_info(context, "chat memory update gate failed, keeping proposal")
        return update, {
            "status": "failed",
            "error": "chat memory update gate failed",
            "post_checks": [],
        }

    label = str(getattr(decision, "label", "")).strip().lower()
    status = str(getattr(decision, "status", "")).strip()
    evidence = [
        str(item).strip()
        for item in getattr(decision, "evidence", [])
        if str(item).strip()
    ]
    evidence_support = (
        str(getattr(decision, "evidence_support", "none")).strip().lower()
    )
    gate_trace: dict[str, Any] = {
        "label": label,
        "status": status,
        "reason": str(getattr(decision, "reason", "")).strip(),
        "confidence": getattr(decision, "confidence", 0.0),
        "evidence": evidence,
        "evidence_support": evidence_support,
        "post_checks": [],
    }
    if label not in {"suppress", "reject", "drop", "no"}:
        if not evidence:
            metadata = dict(update.metadata)
            metadata["suppressed_reason"] = (
                "memory retention gate returned keep without thread evidence"
            )
            if status:
                metadata["retention_status"] = status
            metadata["suppression_confidence"] = getattr(decision, "confidence", 0.0)
            gate_trace["post_checks"].append(
                {
                    "name": "evidence_required",
                    "passed": False,
                    "reason": "keep decision did not include thread evidence",
                }
            )
            return replace(update, should_update=False, metadata=metadata), gate_trace
        if evidence_support != "supports_memory":
            metadata = dict(update.metadata)
            metadata["suppressed_reason"] = (
                "memory retention evidence does not support the proposed memory content"
            )
            if status:
                metadata["retention_status"] = status
            metadata["suppression_confidence"] = getattr(decision, "confidence", 0.0)
            gate_trace["post_checks"].append(
                {
                    "name": "evidence_support_required",
                    "passed": False,
                    "reason": "thread evidence is topic-only or absent",
                }
            )
            return replace(update, should_update=False, metadata=metadata), gate_trace
        gate_trace["post_checks"].append({"name": "evidence_required", "passed": True})
        gate_trace["post_checks"].append(
            {"name": "evidence_support_required", "passed": True}
        )
        retention_mode = (
            str(getattr(decision, "retention_mode", "durable")).strip().lower()
        )
        temporary_expires_at = str(
            getattr(decision, "temporary_expires_at", "")
        ).strip()
        if retention_mode == "temporary":
            if not _is_absolute_iso8601_with_timezone(temporary_expires_at):
                metadata = dict(update.metadata)
                metadata["suppressed_reason"] = (
                    "temporary retention decision requires absolute expires_at"
                )
                if status:
                    metadata["retention_status"] = status
                metadata["suppression_confidence"] = getattr(
                    decision, "confidence", 0.0
                )
                gate_trace["post_checks"].append(
                    {
                        "name": "temporary_expires_at_required",
                        "passed": False,
                        "reason": (
                            "temporary retention decision must provide absolute "
                            "ISO 8601 expires_at with timezone"
                        ),
                    }
                )
                return replace(
                    update, should_update=False, metadata=metadata
                ), gate_trace
            retention = dict(update.retention)
            retention["status"] = "temporary"
            retention["kind"] = "temporary"
            retention["expires_at"] = temporary_expires_at
            if not str(retention.get("reason", "")).strip():
                retention["reason"] = str(getattr(decision, "reason", "")).strip()
            update = replace(update, retention=retention)
            gate_trace["post_checks"].append(
                {"name": "temporary_retention_applied", "passed": True}
            )
            if not update.should_update:
                update = _promote_temporary_update_from_gate(
                    update,
                    payload=payload,
                    decision=decision,
                    expires_at=temporary_expires_at,
                )
                gate_trace["post_checks"].append(
                    {"name": "temporary_update_promoted", "passed": True}
                )
        elif not update.should_update:
            metadata = dict(update.metadata)
            if status:
                metadata["retention_status"] = status
            metadata["retention_reason"] = str(getattr(decision, "reason", "")).strip()
            metadata["retention_evidence"] = evidence
            metadata["retention_confidence"] = getattr(decision, "confidence", 0.0)
            gate_trace["post_checks"].append(
                {
                    "name": "proposal_should_update_false",
                    "passed": True,
                    "reason": "no temporary promotion requested",
                }
            )
            return replace(update, metadata=metadata), gate_trace
        if status == "open_loop" and _open_questions_is_empty(update.memory):
            metadata = dict(update.metadata)
            metadata["suppressed_reason"] = (
                "memory retention status open_loop conflicts with empty Open Questions"
            )
            metadata["retention_status"] = status
            metadata["suppression_confidence"] = getattr(decision, "confidence", 0.0)
            gate_trace["post_checks"].append(
                {
                    "name": "open_questions_required_for_open_loop",
                    "passed": False,
                    "reason": "open_loop decision requires non-empty Open Questions section",
                }
            )
            return replace(update, should_update=False, metadata=metadata), gate_trace
        metadata = dict(update.metadata)
        if status:
            metadata["retention_status"] = status
        metadata["retention_reason"] = str(getattr(decision, "reason", "")).strip()
        metadata["retention_evidence"] = evidence
        metadata["retention_confidence"] = getattr(decision, "confidence", 0.0)
        return replace(update, metadata=metadata), gate_trace

    metadata = dict(update.metadata)
    metadata["suppressed_reason"] = str(getattr(decision, "reason", "")).strip()
    if status:
        metadata["retention_status"] = status
    metadata["suppression_confidence"] = getattr(decision, "confidence", 0.0)
    gate_trace["post_checks"].append({"name": "gate_suppressed", "passed": True})
    return replace(update, should_update=False, metadata=metadata), gate_trace


def _is_absolute_iso8601_with_timezone(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _promote_temporary_update_from_gate(
    update: MemoryUpdate,
    *,
    payload: dict[str, Any],
    decision: Any,
    expires_at: str,
) -> MemoryUpdate:
    if update.should_update:
        return update
    thread_context = payload.get("thread_context", {})
    latest_focus = (
        str(thread_context.get("latest_focus", "")).strip()
        if isinstance(thread_context, dict)
        else ""
    )
    thread_topic = (
        str(thread_context.get("thread_topic", "")).strip()
        if isinstance(thread_context, dict)
        else ""
    )
    title = update.title.strip() or thread_topic or "Temporary Context"
    topic_id = update.topic_id.strip() or _topic_id(title) or "temporary-context"
    summary = update.summary.strip() or (f"Temporary context valid until {expires_at}.")
    decision_reason = str(getattr(decision, "reason", "")).strip()
    memory = update.memory.strip()
    if not memory:
        focus_line = latest_focus or "Thread contains time-limited instruction."
        reason_line = decision_reason or "This context is temporary."
        memory = "\n".join(
            [
                f"# {title}",
                "",
                "## Summary",
                f"- Temporary instruction valid until {expires_at}.",
                "",
                "## Decisions",
                "- None",
                "",
                "## Open Questions",
                "- None",
                "",
                "## Current Direction",
                f"- {focus_line}",
                f"- {reason_line}",
            ]
        )
    metadata = dict(update.metadata)
    metadata.setdefault("reason", decision_reason or "temporary instruction")
    metadata["temporary_promoted_from_should_update_false"] = True
    return replace(
        update,
        should_update=True,
        topic_id=topic_id,
        title=title,
        summary=summary,
        memory=memory,
        metadata=metadata,
    )


def _memory_decision_trace_input(payload: dict[str, Any]) -> dict[str, Any]:
    memory_context = payload.get("memory_context", {})
    items = memory_context.get("items", []) if isinstance(memory_context, dict) else []
    return {
        "agent_profile_excerpt": _short_json(payload.get("agent_profile", {}), 1000),
        "thread_context": payload.get("thread_context", {}),
        "event_time": payload.get("event_time", {}),
        "current_time": payload.get("current_time", {}),
        "thread_messages": [
            {
                "author": str(message.get("author", "")),
                "content_excerpt": str(message.get("content", ""))[:500],
            }
            for message in payload.get("thread_messages", [])
            if isinstance(message, dict)
        ],
        "reply_text_excerpt": str(payload.get("reply_text", ""))[:1000],
        "memory_context_summary": {
            "status": memory_context.get("status", "")
            if isinstance(memory_context, dict)
            else "",
            "hit_count": len(items) if isinstance(items, list) else 0,
            "hit_ids": [
                str(item.get("id", ""))
                for item in items
                if isinstance(item, dict) and item.get("id")
            ],
        },
    }


def _short_json(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text[:limit]


def _open_questions_is_empty(memory_text: str) -> bool:
    text = memory_text.strip()
    if not text:
        return True
    match = re.search(r"^##\s+Open Questions\s*$", text, flags=re.MULTILINE)
    if not match:
        return True
    body_start = match.end()
    next_heading = re.search(r"^##\s+", text[body_start:], flags=re.MULTILINE)
    body = (
        text[body_start : body_start + next_heading.start()]
        if next_heading
        else text[body_start:]
    )
    normalized = " ".join(body.split()).strip().lower()
    return normalized in {"", "none", "- none", "* none"}


def _memory_update_proposal_payload(result: Any) -> Any:
    if isinstance(result, dict):
        return result
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return {
        "should_update": getattr(result, "should_update", False),
        "topic_id": getattr(result, "topic_id", ""),
        "title": getattr(result, "title", ""),
        "summary": getattr(result, "summary", ""),
        "memory": getattr(result, "memory", ""),
        "forget_item_ids": getattr(result, "forget_item_ids", []),
        "forget_reason": getattr(result, "forget_reason", ""),
        "retention": getattr(result, "retention", {}),
        "reason": getattr(result, "reason", ""),
        "confidence": getattr(result, "confidence", 0.0),
    }


def _memory_forget_requests(
    result: Any,
    *,
    memory_update: MemoryUpdate,
    memory_context: MemoryContext,
    context: Any,
    event: ChatEvent | None,
) -> list[MemoryForgetRequest]:
    person = getattr(context, "person", None)
    person_id = str(getattr(person, "person_id", "")).strip()
    forget_ids = _memory_forget_item_ids(result)
    deduped = list(dict.fromkeys(item_id for item_id in forget_ids if item_id))
    reason = _memory_forget_reason(result)
    return [
        MemoryForgetRequest(
            person_id=person_id,
            item_id=item_id,
            reason=reason,
            source=_memory_source(event),
            scope={"person_id": person_id} if person_id else {},
            metadata={"operation": "chat_memory_update"},
        )
        for item_id in deduped
    ]


def _memory_forget_item_ids(result: Any) -> list[str]:
    if isinstance(result, dict):
        value = result.get("forget_item_ids", [])
    else:
        value = getattr(result, "forget_item_ids", [])
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _memory_forget_reason(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("forget_reason", "")).strip()
    return str(getattr(result, "forget_reason", "")).strip()


def _topic_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "-", value.lower()).strip("-")
    return normalized[:80].strip("-") if normalized else ""


def _memory_source(event: ChatEvent | None) -> dict[str, str]:
    if event is None:
        return {}
    return {
        "type": "slack_thread",
        "service": "slack",
        "channel": event.channel_id,
        "thread_ts": event.thread_ts,
    }


def _memory_update_metadata(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        metadata = result.get("metadata")
        payload = metadata if isinstance(metadata, dict) else {}
        reason = result.get("reason")
        confidence = result.get("confidence")
    else:
        metadata = getattr(result, "metadata", None)
        payload = metadata if isinstance(metadata, dict) else {}
        reason = getattr(result, "reason", None)
        confidence = getattr(result, "confidence", None)
    normalized = dict(payload)
    if reason is not None:
        normalized["reason"] = reason
    if confidence is not None:
        normalized["confidence"] = confidence
    return normalized


def _memory_update_retention(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        retention = result.get("retention")
    else:
        retention = getattr(result, "retention", None)
    if isinstance(retention, dict):
        return dict(retention)
    model_dump = getattr(retention, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _with_default_memory_retention(
    update: MemoryUpdate, *, event: ChatEvent | None
) -> MemoryUpdate:
    if not update.should_update:
        return update
    retention = dict(update.retention)
    if "kind" not in retention:
        status = str(retention.get("status", "")).strip().lower()
        retention["kind"] = "temporary" if status == "temporary" else "current_fact"
    # Transition memory must reference the subject current memory item.
    # If missing, treat it as a current_fact payload instead of history.
    if (
        str(retention.get("kind", "")).strip().lower() == "transition"
        and not str(retention.get("subject_item_id", "")).strip()
    ):
        retention["kind"] = "current_fact"
    if "status" not in retention:
        retention["status"] = "active"
    effective_at = str(retention.get("effective_at", "")).strip()
    if not effective_at:
        event_time = _chat_event_time_payload(event) if event is not None else {}
        retention["effective_at"] = (
            str(event_time.get("iso", "")).strip()
            or str(_current_time_payload().get("iso", "")).strip()
        )
    return replace(update, retention=retention)


def _is_temporary_retention(retention: dict[str, Any]) -> bool:
    status = str(retention.get("status", "")).strip().lower()
    kind = str(retention.get("kind", "")).strip().lower()
    return status == "temporary" or kind == "temporary"


def _ensure_temporary_topic_id(
    update: MemoryUpdate,
    memory_context: MemoryContext,
    *,
    event: ChatEvent | None,
) -> MemoryUpdate:
    if not update.should_update:
        return update
    if not _is_temporary_retention(update.retention):
        return update
    base_topic_id = _topic_id(update.topic_id or update.title) or "temporary-memory"
    existing_ids = {item.id for item in memory_context.items if item.id}
    if base_topic_id not in existing_ids:
        return replace(update, topic_id=base_topic_id)
    event_token = _event_token(event, fallback=base_topic_id)
    return replace(update, topic_id=f"{base_topic_id}-temporary-{event_token}")


def _coalesce_memory_topic(
    update: MemoryUpdate, memory_context: MemoryContext
) -> MemoryUpdate:
    if not update.should_update:
        return update
    if not memory_context.items:
        return update
    items_by_id = {item.id: item for item in memory_context.items if item.id}
    existing_ids = set(items_by_id.keys())
    if update.topic_id in existing_ids:
        canonical_id = _canonical_topic_id(update.topic_id, items_by_id)
        if canonical_id != update.topic_id:
            canonical = items_by_id.get(canonical_id)
            return replace(
                update,
                topic_id=canonical_id,
                title=(canonical.title if canonical is not None else update.title),
            )
        return update
    if len(memory_context.items) != 1:
        return update
    candidate = memory_context.items[0]
    if not candidate.id:
        return update
    canonical_candidate_id = _canonical_topic_id(candidate.id, items_by_id)
    canonical_candidate = items_by_id.get(canonical_candidate_id)
    topic_similarity = _topic_similarity(update.topic_id, candidate.id)
    if (
        topic_similarity < _TOPIC_COALESCE_SIMILARITY_THRESHOLD
        and not _shares_topic_stem(update.topic_id, candidate.id)
    ):
        return update
    title_id = _topic_id(update.title.strip()) if update.title.strip() else ""
    if (
        title_id
        and _topic_similarity(title_id, candidate.id)
        < _TOPIC_COALESCE_SIMILARITY_THRESHOLD
    ):
        return replace(update, topic_id=canonical_candidate_id)
    if canonical_candidate is not None:
        return replace(
            update,
            topic_id=canonical_candidate_id,
            title=canonical_candidate.title or update.title,
        )
    if canonical_candidate_id != candidate.id:
        return replace(update, topic_id=canonical_candidate_id)
    return replace(update, topic_id=candidate.id, title=candidate.title or update.title)


def _canonical_topic_id(item_id: str, items_by_id: dict[str, MemoryItem]) -> str:
    visited: set[str] = set()
    current = item_id
    while current and current not in visited:
        visited.add(current)
        item = items_by_id.get(current)
        if item is None:
            return current
        kind = str(item.retention.get("kind", "")).strip().lower()
        subject = str(item.retention.get("subject_item_id", "")).strip()
        if kind != "transition" or not subject:
            return current
        current = subject
    return item_id


def _topic_similarity(left: str, right: str) -> float:
    left_tokens = {token for token in _topic_id(left).split("-") if token}
    right_tokens = {token for token in _topic_id(right).split("-") if token}
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)


def _shares_topic_stem(left: str, right: str) -> bool:
    left_tokens = [token for token in _topic_id(left).split("-") if token]
    right_tokens = [token for token in _topic_id(right).split("-") if token]
    if (
        len(left_tokens) < _TOPIC_STEM_TOKEN_COUNT
        or len(right_tokens) < _TOPIC_STEM_TOKEN_COUNT
    ):
        return False
    return (
        left_tokens[:_TOPIC_STEM_TOKEN_COUNT] == right_tokens[:_TOPIC_STEM_TOKEN_COUNT]
    )


def _memory_transition_update(
    *,
    memory_update: MemoryUpdate,
    memory_context: MemoryContext,
    context: Any,
    event: ChatEvent | None,
) -> MemoryUpdate | None:
    if not memory_update.should_update:
        return None
    if _is_temporary_retention(memory_update.retention):
        return None
    updated_item_id = _topic_id(memory_update.topic_id or memory_update.title)
    previous = next(
        (item for item in memory_context.items if item.id == updated_item_id),
        None,
    )
    proxy_from_transition = False
    if previous is None:
        previous = next(
            (
                item
                for item in memory_context.items
                if str(item.retention.get("kind", "")).strip().lower() == "transition"
                and str(item.retention.get("subject_item_id", "")).strip()
                == updated_item_id
            ),
            None,
        )
        proxy_from_transition = previous is not None
    if previous is None:
        return None
    if (
        not proxy_from_transition
        and str(previous.retention.get("kind", "")).strip().lower() == "transition"
    ):
        # Never stack transition on top of transition.
        return None
    event_time = _chat_event_time_payload(event) if event is not None else {}
    effective_at = (
        str(event_time.get("iso", "")).strip()
        or str(_current_time_payload().get("iso", "")).strip()
    )
    event_token = _event_token(event, fallback=updated_item_id)
    title = f"{memory_update.title or previous.title} Change"
    reason = str(memory_update.metadata.get("reason", "")).strip()
    change_lines = [
        f"- Subject memory: `{updated_item_id}`",
        f"- Previous: {previous.summary or previous.title}",
        f"- Current: {memory_update.summary or memory_update.title}",
    ]
    if effective_at:
        change_lines.append(f"- Effective at: {effective_at}")
    if reason:
        change_lines.append(f"- Reason: {reason}")
    memory = "\n".join(
        [
            f"# {title}",
            "",
            "## Change",
            *change_lines,
            "",
            "## Previous Memory Excerpt",
            previous.content.strip()[:1200],
            "",
            "## Current Memory Excerpt",
            memory_update.memory.strip()[:1200],
        ]
    )
    person = getattr(context, "person", None)
    person_id = str(getattr(person, "person_id", "")).strip()
    return MemoryUpdate(
        should_update=True,
        topic_id=f"{updated_item_id}-transition-{event_token}",
        title=title,
        summary=(
            f"{previous.summary or previous.title} changed to "
            f"{memory_update.summary or memory_update.title}"
        ),
        memory=memory,
        source=_memory_source(event),
        scope={"person_id": person_id} if person_id else {},
        metadata={
            "operation": "memory_evolution",
            "subject_item_id": updated_item_id,
            "previous_item_id": previous.id,
            "current_item_id": updated_item_id,
            "effective_at": effective_at,
            "reason": reason,
        },
        retention={
            "status": "active",
            "kind": "transition",
            "subject_item_id": updated_item_id,
            "effective_at": effective_at,
            "reason": reason,
        },
    )


def _chat_event_time_payload(event: ChatEvent) -> dict[str, str]:
    payload = {"message_ts": event.message_ts}
    if parsed := _datetime_from_chat_ts(event.message_ts):
        payload["iso"] = parsed.isoformat(timespec="seconds")
        payload["date"] = parsed.date().isoformat()
        payload["timezone"] = str(parsed.tzinfo or "")
    return payload


def _current_time_payload() -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "timezone": str(now.tzinfo or ""),
    }


def _datetime_from_chat_ts(value: str) -> datetime | None:
    try:
        timestamp = float(value)
    except ValueError:
        return None
    if timestamp < _MIN_CHAT_TIMESTAMP:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone()


def _event_token(event: ChatEvent | None, *, fallback: str) -> str:
    if event is None:
        return _topic_id(fallback)[:12] or "unknown"
    return _topic_id(event.message_ts or event.event_id or fallback)[:24] or "unknown"


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
        _log_info(
            context, "chat reply intent classification failed, defaulting to answer"
        )
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
        _log_info(
            context,
            "chat thread context classification failed, defaulting to empty context",
        )
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

    author_labels = await _build_author_labels(
        context, self_user_id, event, thread_messages[-20:]
    )
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
    return IncomingChatEvent.from_shared_state(
        shared_state.get(INCOMING_CHAT_EVENT_KEY)
    )
