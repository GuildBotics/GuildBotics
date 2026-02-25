from __future__ import annotations

from copy import deepcopy
from typing import Any

from guildbotics.commands.utils import stringify_output
from guildbotics.drivers.chat_event_source import (
    ChatEventSource,
)
from guildbotics.integrations.chat_service import ChatService
from guildbotics.integrations.chat_profile import (
    get_chat_slack_base_url,
    get_chat_subscriptions,
)
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_socket_mode_chat_event_source import (
    SocketModeChatEventSource,
)
from guildbotics.templates.commands.workflows.chat.policies.models import (
    PolicyEvent,
    PolicyInput,
    ProcessingState,
    ThreadContext,
)
from guildbotics.templates.commands.workflows.chat.policies.should_react import (
    ShouldReactPolicy,
)


async def main(
    context: Any,
    chat_service: ChatService | None = None,
    state_store: ConversationStateStore | None = None,
    event_source: ChatEventSource | None = None,
) -> None:
    """Monitor configured chat subscriptions and react to incoming messages."""
    chat_service = chat_service or context.get_chat_service()
    state_store = state_store or FileConversationStateStore()
    policy = ShouldReactPolicy()

    subscriptions = get_chat_subscriptions(context.person)
    if not subscriptions:
        return
    subscriptions = await _resolve_subscription_channels(context, chat_service, subscriptions)
    if not subscriptions:
        return

    identity = await chat_service.get_bot_identity()
    grouped = _group_subscriptions_by_event_source(subscriptions)
    for source_kind, group_subs in grouped.items():
        source = event_source if event_source is not None else _create_event_source_for_kind(
            context=context,
            source_kind=source_kind,
            chat_service=chat_service,
            state_store=state_store,
        )
        cycle_events = await source.fetch_events(
            person_id=context.person.person_id,
            subscriptions=group_subs,
        )
        try:
            for item in cycle_events:
                await _handle_event(
                    context=context,
                    chat_service=chat_service,
                    state_store=state_store,
                    policy=policy,
                    service_name=item.service_name,
                    channel_id=item.channel_id,
                    identity_user_id=identity.user_id,
                    event=item.event,
                )
                source.mark_processed(person_id=context.person.person_id, item=item)
        finally:
            source.finalize_cycle(person_id=context.person.person_id)


async def _handle_event(
    *,
    context: Any,
    chat_service: ChatService,
    state_store: ConversationStateStore,
    policy: ShouldReactPolicy,
    service_name: str,
    channel_id: str,
    identity_user_id: str,
    event: Any,
) -> None:
    thread_state = state_store.load_thread_state(
        service_name, context.person.person_id, channel_id, event.thread_ts
    )
    channel_state = state_store.load_channel_cursor(
        service_name, context.person.person_id, channel_id
    )
    already_processed = event.event_id in set(channel_state.processed_event_ids)
    if not already_processed and event.is_message and not event.is_edit_or_delete:
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

    policy_input = PolicyInput(
        self_person_id=context.person.person_id,
        self_user_id=identity_user_id,
        event=PolicyEvent(
            event_id=event.event_id,
            channel_id=event.channel_id,
            message_ts=event.message_ts,
            thread_ts=event.thread_ts,
            author_id=event.author_id,
            text=event.text,
            mentions=list(event.mentions),
            is_message=event.is_message,
            is_edit_or_delete=event.is_edit_or_delete,
            is_bot_message=event.is_bot_message,
            is_from_self=(event.author_id == identity_user_id),
            is_in_subscribed_channel=(event.channel_id == channel_id),
            is_thread_reply=event.is_thread_reply,
        ),
        thread_context=ThreadContext(
            participants=set(thread_state.participants),
            last_bot_replier_id=thread_state.last_bot_replier_id,
            bot_auto_turn_count=0,
            too_many_recent_bot_replies=False,
            thread_claimed_by_other=thread_state.thread_claimed_by_other,
        ),
        state=ProcessingState(
            already_processed=already_processed,
            response_expected=thread_state.response_expected,
        ),
    )
    decision = policy.evaluate(policy_input)

    if hasattr(context, "logger"):
        try:
            context.logger.info(
                "chat decision=%s reason=%s channel=%s thread=%s event=%s",
                decision.decision,
                decision.reason,
                channel_id,
                event.thread_ts,
                event.event_id,
            )
        except Exception:
            pass

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
    reply_text = await _build_reply_text(context, event, thread_messages)
    if not reply_text.strip():
        return

    post_result = await chat_service.post_message(channel_id, reply_text, thread_ts=event.thread_ts)
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
            text=reply_text,
            mentions=[],
            is_bot_message=True,
        ),
    )
    thread_state.participants.add(context.person.person_id)
    thread_state.last_bot_replier_id = context.person.person_id
    thread_state.response_expected = True
    state_store.save_thread_state(
        service_name, context.person.person_id, channel_id, event.thread_ts, thread_state
    )

async def _build_reply_text(
    context: Any, event: Any, thread_messages: list[ThreadMessageState]
) -> str:
    text = await _build_reply_text_via_command(context, event, thread_messages)
    if text.strip():
        return text
    # Fallback for contexts without invoker / command failures.
    return _build_reply_text_fallback(context, event, thread_messages)


async def _build_reply_text_via_command(
    context: Any, event: Any, thread_messages: list[ThreadMessageState]
) -> str:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        return ""

    payload = {
        "channel_id": getattr(event, "channel_id", ""),
        "thread_ts": getattr(event, "thread_ts", ""),
        "message_ts": getattr(event, "message_ts", ""),
        "latest_message": {
            "author_id": getattr(event, "author_id", None),
            "text": getattr(event, "text", ""),
            "mentions": list(getattr(event, "mentions", []) or []),
            "is_thread_reply": bool(getattr(event, "is_thread_reply", False)),
        },
        "thread_messages": [
            {
                "message_ts": msg.message_ts,
                "author_id": msg.author_id,
                "text": msg.text,
                "mentions": list(msg.mentions),
                "is_bot_message": msg.is_bot_message,
            }
            for msg in thread_messages[-20:]
        ],
    }

    transcript_lines = []
    for msg in thread_messages[-20:]:
        speaker = "bot" if msg.is_bot_message else (msg.author_id or "user")
        transcript_lines.append(f"[{speaker}] {msg.text}")
    if not transcript_lines:
        transcript_lines.append(str(getattr(event, "text", "") or ""))
    transcript = "\n".join(transcript_lines)

    old_pipe = getattr(context, "pipe", "")
    has_shared_state = isinstance(getattr(context, "shared_state", None), dict)
    old_shared_state = deepcopy(context.shared_state) if has_shared_state else None
    try:
        if has_shared_state:
            context.shared_state["chat_reply_input"] = payload
        if hasattr(context, "pipe"):
            context.pipe = transcript
        result = await invoke("workflows/chat_reply")
        return stringify_output(result).strip()
    except Exception:
        _log_info(
            context,
            "chat reply command failed, falling back to placeholder reply generation",
        )
        return ""
    finally:
        if has_shared_state and old_shared_state is not None:
            context.shared_state.clear()
            context.shared_state.update(old_shared_state)
        if hasattr(context, "pipe"):
            context.pipe = old_pipe


def _build_reply_text_fallback(
    context: Any, event: Any, thread_messages: list[ThreadMessageState]
) -> str:
    latest_text = ""
    for msg in reversed(thread_messages):
        if not msg.is_bot_message:
            latest_text = msg.text.strip()
            if latest_text:
                break
    if not latest_text:
        latest_text = (getattr(event, "text", "") or "").strip()
    person_name = getattr(getattr(context, "person", None), "name", "Agent")
    return f"{person_name}: {latest_text}"


async def _resolve_subscription_channels(
    context: Any, chat_service: ChatService, subscriptions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for sub in subscriptions:
        if not isinstance(sub, dict):
            continue
        item = dict(sub)
        channel_id = str(item.get("channel_id", "")).strip()
        if channel_id:
            resolved.append(item)
            continue
        channel_name = str(item.get("channel_name", "")).strip()
        if not channel_name:
            continue
        resolved_id = await chat_service.resolve_channel_id(channel_name)
        if not resolved_id:
            _log_info(
                context,
                "chat subscription skipped: channel_name=%s could not be resolved "
                "(check channel name, bot channel membership, and Slack scopes "
                "channels:read/groups:read)",
                channel_name,
            )
            continue
        item["channel_id"] = resolved_id
        resolved.append(item)
    return resolved


def _group_subscriptions_by_event_source(
    subscriptions: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for sub in subscriptions:
        source_kind = (
            str(sub.get("event_source", "socket_mode")).strip().lower() or "socket_mode"
        )
        out.setdefault(source_kind, []).append(sub)
    return out


def _create_event_source_for_kind(
    *,
    context: Any,
    source_kind: str,
    chat_service: ChatService,
    state_store: ConversationStateStore,
) -> ChatEventSource:
    if source_kind == "socket_mode":
        if hasattr(context, "get_chat_event_source"):
            try:
                return context.get_chat_event_source(source_kind="socket_mode")
            except TypeError:
                return context.get_chat_event_source()
        return SocketModeChatEventSource(
            logger=getattr(context, "logger", None),
            person=context.person,
            state_store=state_store,
            base_url=get_chat_slack_base_url(context.person),
        )
    raise ValueError(
        "Unsupported Slack chat event_source for chat_conversation_workflow: "
        f"'{source_kind}'. Use 'socket_mode'."
    )


def _log_info(context: Any, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        logger.info(msg, *args)
    except Exception:
        return
