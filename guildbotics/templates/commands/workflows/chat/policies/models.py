from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DecisionKind = Literal["reply", "react_only", "ignore"]


@dataclass(slots=True)
class PolicyLimits:
    max_bot_auto_turns: int = 2
    bot_reply_cooldown_seconds: int = 10


@dataclass(slots=True)
class PolicyEvent:
    event_id: str
    channel_id: str
    message_ts: str
    thread_ts: str
    author_id: str | None
    text: str
    mentions: list[str]
    is_message: bool = True
    is_edit_or_delete: bool = False
    is_bot_message: bool = False
    is_from_self: bool = False
    is_in_subscribed_channel: bool = True
    is_thread_reply: bool = False


@dataclass(slots=True)
class ThreadContext:
    participants: set[str] = field(default_factory=set)
    last_bot_replier_id: str | None = None
    last_message_author_id: str | None = None
    bot_auto_turn_count: int = 0
    too_many_recent_bot_replies: bool = False
    thread_claimed_by_other: bool = False


@dataclass(slots=True)
class ProcessingState:
    already_processed: bool = False
    response_expected: bool = True


@dataclass(slots=True)
class PolicyInput:
    self_person_id: str
    self_user_id: str
    event: PolicyEvent
    thread_context: ThreadContext
    state: ProcessingState
    limits: PolicyLimits = field(default_factory=PolicyLimits)


@dataclass(slots=True)
class PolicyDecision:
    decision: DecisionKind
    reason: str
    reaction: str | None = None
    response_expected: bool | None = None
