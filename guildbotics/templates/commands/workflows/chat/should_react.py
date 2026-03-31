from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from guildbotics.integrations.chat_service import (
    SEMANTIC_REACTIONS,
    ChatEvent,
    SemanticReaction,
)


@dataclass(slots=True)
class DecisionResult:
    decision: str
    reason: str
    reaction: SemanticReaction | None = None


@dataclass(slots=True)
class ReactionThreadContext:
    participants: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ReactionInput:
    self_person_id: str
    self_user_id: str
    event: ChatEvent
    thread_context: ReactionThreadContext = field(default_factory=ReactionThreadContext)
    thread_messages: list[dict[str, str]] = field(default_factory=list)
    already_processed: bool = False


async def main(
    context: Any,
    *,
    channel_type: str = "chat",
    reaction_input: ReactionInput,
) -> dict[str, Any]:
    """Decide whether to react/reply to an incoming event.

    This workflow-level command is intentionally channel-agnostic at the entrypoint.
    Currently, it supports chat policy evaluation and returns a normalized mapping:
    {"decision", "reason", "reaction"}.
    """
    kind = str(channel_type or "chat").strip().casefold()
    if kind != "chat":
        raise ValueError(f"Unsupported channel_type for should_react: {channel_type}")

    if reaction_input.already_processed:
        return {
            "decision": "ignore",
            "reason": "already_processed",
            "reaction": None,
        }

    decision = await decide_reaction_with_context(context, reaction_input)
    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "reaction": decision.reaction,
    }


def decide_reaction(data: ReactionInput) -> DecisionResult:
    targeted_to_me = bool(data.self_user_id and data.self_user_id in data.event.mentions)
    targeted_to_others = any(user_id != data.self_user_id for user_id in data.event.mentions)
    continuation = (
        data.event.is_thread_reply
        and data.self_person_id in data.thread_context.participants
    )

    if data.event.is_from_user(data.self_user_id):
        return _ignore("self_message")
    if targeted_to_me:
        return _reply("explicit_mention")
    if targeted_to_others:
        return _ignore("mentioned_other_agent_only")
    if continuation:
        return _followup_decision(data)
    return _ignore("no_trigger")


def _followup_decision(data: ReactionInput) -> DecisionResult:
    return DecisionResult(decision="reply", reason="thread_followup_pending_llm")


async def decide_reaction_with_context(context: Any, data: ReactionInput) -> DecisionResult:
    decision = decide_reaction(data)
    if decision.reason != "thread_followup_pending_llm":
        return decision
    return await _decide_thread_followup_with_llm(context, data)


async def _decide_thread_followup_with_llm(
    context: Any, data: ReactionInput
) -> DecisionResult:
    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        return _reply("thread_followup")

    latest_message = (
        dict(data.thread_messages[-1])
        if data.thread_messages
        else {
            "content": data.event.text,
            "author": data.event.author_id or "",
            "author_type": "Assistant" if data.event.is_bot_message else "User",
        }
    )
    thread_messages_payload = [dict(item) for item in data.thread_messages[-20:]]
    payload = {
        "latest_message": latest_message,
        "thread_messages": thread_messages_payload,
    }
    transcript_lines = [
        f"[{item.get('author', '')}] {item.get('content', '')}".strip()
        for item in thread_messages_payload
    ]
    transcript = "\n".join(line for line in transcript_lines if line.strip())

    old_pipe = getattr(context, "pipe", "")
    has_shared_state = isinstance(getattr(context, "shared_state", None), dict)
    old_shared_state = deepcopy(context.shared_state) if has_shared_state else None
    try:
        if has_shared_state:
            context.shared_state["chat_should_reply_input"] = payload
        if hasattr(context, "pipe"):
            context.pipe = transcript
        result = await invoke("workflows/chat/chat_followup_should_reply")
    except Exception:
        return _reply("thread_followup")
    finally:
        if has_shared_state and old_shared_state is not None:
            context.shared_state.clear()
            context.shared_state.update(old_shared_state)
        if hasattr(context, "pipe"):
            context.pipe = old_pipe

    normalized = _normalize_followup_result(result)
    if normalized is None:
        return _reply("thread_followup")
    return normalized


def _normalize_followup_result(result: Any) -> DecisionResult | None:
    if isinstance(result, dict):
        label = str(result.get("label", "")).strip()
        reason = str(result.get("reason", "")).strip()
        reaction = result.get("reaction")
    else:
        label = str(getattr(result, "label", "")).strip()
        reason = str(getattr(result, "reason", "")).strip()
        reaction = getattr(result, "reaction", None)

    if label == "reply":
        return _reply(reason or "thread_followup")
    if label == "react_only":
        normalized_reaction = _normalize_semantic_reaction(reaction)
        if normalized_reaction is None:
            return None
        return DecisionResult(
            decision="react_only",
            reason=reason or "thread_followup_react_only",
            reaction=normalized_reaction,
        )
    if label == "ignore":
        return _ignore(reason or "thread_followup_ignored")
    return None


def _reply(reason: str) -> DecisionResult:
    return DecisionResult(decision="reply", reason=reason)


def _ignore(reason: str) -> DecisionResult:
    return DecisionResult(decision="ignore", reason=reason)


def _normalize_semantic_reaction(reaction: Any) -> SemanticReaction | None:
    value = str(reaction or "").strip()
    if value in SEMANTIC_REACTIONS:
        return cast(SemanticReaction, value)
    return None
