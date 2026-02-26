from __future__ import annotations

from dataclasses import replace

import pytest

from guildbotics.templates.commands.workflows.chat.policies.models import (
    PolicyEvent,
    PolicyInput,
    ProcessingState,
    ThreadContext,
)
from guildbotics.templates.commands.workflows.chat.policies.should_react import (
    ShouldReactPolicy,
)


SELF_USER_ID = "U_SELF"
OTHER_AGENT_USER_ID = "U_BOB"
OUTSIDER_USER_ID = "U_OTHER"
SELF_PERSON_ID = "alice"
OTHER_PERSON_ID = "bob"


def _base_input() -> PolicyInput:
    return PolicyInput(
        self_person_id=SELF_PERSON_ID,
        self_user_id=SELF_USER_ID,
        event=PolicyEvent(
            event_id="E1",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id=OUTSIDER_USER_ID,
            text="hello",
            mentions=[],
            is_thread_reply=False,
        ),
        thread_context=ThreadContext(),
        state=ProcessingState(),
    )


def _with(input: PolicyInput, **kwargs) -> PolicyInput:
    event = kwargs.pop("event", None)
    thread_context = kwargs.pop("thread_context", None)
    state = kwargs.pop("state", None)
    out = input
    if event is not None:
        out = replace(out, event=replace(out.event, **event))
    if thread_context is not None:
        out = replace(out, thread_context=replace(out.thread_context, **thread_context))
    if state is not None:
        out = replace(out, state=replace(out.state, **state))
    if kwargs:
        out = replace(out, **kwargs)
    return out


CASES = [
    (
        "SRP-001",
        {"event": {"is_message": False}},
        ("ignore", "not_message", None),
    ),
    (
        "SRP-002",
        {"event": {"is_edit_or_delete": True}},
        ("ignore", "unsupported_message_subtype", None),
    ),
    (
        "SRP-003",
        {"event": {"is_in_subscribed_channel": False}},
        ("ignore", "channel_not_subscribed", None),
    ),
    (
        "SRP-004",
        {"state": {"already_processed": True}},
        ("ignore", "already_processed", None),
    ),
    (
        "SRP-005",
        {"event": {"is_from_self": True}},
        ("ignore", "self_message", None),
    ),
    (
        "SRP-006",
        {"event": {"is_bot_message": True}},
        ("ignore", "bot_message_ignored_in_mvp", None),
    ),
    (
        "SRP-007",
        {"thread_context": {"bot_auto_turn_count": 2}},
        ("react_only", "bot_loop_limit", "hand"),
    ),
    (
        "SRP-008",
        {"thread_context": {"too_many_recent_bot_replies": True}},
        ("react_only", "bot_reply_cooldown", "hourglass_flowing_sand"),
    ),
    (
        "SRP-009",
        {"event": {"mentions": [SELF_USER_ID]}},
        ("reply", "explicit_mention", None),
    ),
    (
        "SRP-010",
        {"event": {"mentions": [OTHER_AGENT_USER_ID]}},
        ("ignore", "mentioned_other_agent_only", None),
    ),
    (
        "SRP-010b",
        {"event": {"mentions": [OUTSIDER_USER_ID]}},
        ("ignore", "mentioned_other_agent_only", None),
    ),
    (
        "SRP-011",
        {"event": {"mentions": [SELF_USER_ID, OTHER_AGENT_USER_ID]}},
        ("reply", "explicit_mention", None),
    ),
    (
        "SRP-012",
        {
            "event": {"is_thread_reply": True},
            "thread_context": {
                "participants": {SELF_PERSON_ID},
                "last_bot_replier_id": SELF_PERSON_ID,
            },
            "state": {"response_expected": True},
        },
        ("reply", "thread_followup", None),
    ),
    (
        "SRP-013",
        {
            "event": {"is_thread_reply": True},
            "thread_context": {
                "participants": {SELF_PERSON_ID},
                "last_bot_replier_id": SELF_PERSON_ID,
            },
            "state": {"response_expected": False},
        },
        ("ignore", "thread_followup_no_response_expected", None),
    ),
    (
        "SRP-014",
        {
            "event": {"is_thread_reply": True},
            "thread_context": {
                "participants": set(),
                "last_bot_replier_id": SELF_PERSON_ID,
            },
        },
        ("ignore", "no_trigger", None),
    ),
    (
        "SRP-015",
        {
            "event": {"is_thread_reply": True},
            "thread_context": {
                "participants": {SELF_PERSON_ID},
                "last_bot_replier_id": OTHER_PERSON_ID,
            },
        },
        ("ignore", "no_trigger", None),
    ),
    (
        "SRP-016",
        {"thread_context": {"thread_claimed_by_other": True}},
        ("react_only", "thread_claimed_by_other", "eyes"),
    ),
]


@pytest.mark.parametrize(("case_id", "changes", "expected"), CASES)
def test_should_react_policy_cases(case_id, changes, expected):
    policy = ShouldReactPolicy()
    decision = policy.evaluate(_with(_base_input(), **changes))
    assert (decision.decision, decision.reason, decision.reaction) == expected, case_id


def test_thread_claim_does_not_override_explicit_mention():
    policy = ShouldReactPolicy()
    decision = policy.evaluate(
        _with(
            _base_input(),
            event={"mentions": [SELF_USER_ID]},
            thread_context={"thread_claimed_by_other": True},
        )
    )
    assert decision.decision == "reply"
    assert decision.reason == "explicit_mention"
