from __future__ import annotations

import types

import pytest

from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.templates.commands.workflows.chat.should_react import (
    ReactionInput,
    ReactionThreadContext,
)
from guildbotics.templates.commands.workflows.chat import should_react


def _reaction_input(*, mentions: list[str] | None = None) -> ReactionInput:
    return ReactionInput(
        self_person_id="alice",
        self_user_id="U_ALICE",
        event=ChatEvent(
            event_id="E1",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_USER",
            text="hello",
            mentions=list(mentions or []),
            is_bot_message=False,
            is_thread_reply=False,
        ),
        thread_context=ReactionThreadContext(participants=set()),
        thread_messages=[
            {
                "content": "hello",
                "author": "user_1",
                "author_type": "User",
            }
        ],
        already_processed=False,
    )


@pytest.mark.asyncio
async def test_should_react_chat_returns_reply_for_explicit_mention():
    ctx = types.SimpleNamespace()
    result = await should_react.main(
        ctx,
        channel_type="chat",
        reaction_input=_reaction_input(mentions=["U_ALICE"]),
    )
    assert result["decision"] == "reply"
    assert result["reason"] == "explicit_mention"


@pytest.mark.asyncio
async def test_should_react_chat_returns_ignore_when_not_triggered():
    ctx = types.SimpleNamespace()
    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=_reaction_input()
    )
    assert result["decision"] == "ignore"
    assert result["reason"] == "no_trigger"


@pytest.mark.asyncio
async def test_should_react_chat_ignores_already_processed_before_decision():
    ctx = types.SimpleNamespace()
    reaction_input = _reaction_input(mentions=["U_ALICE"])
    reaction_input.already_processed = True
    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )
    assert result["decision"] == "ignore"
    assert result["reason"] == "already_processed"


@pytest.mark.asyncio
async def test_should_react_raises_for_unsupported_channel_type():
    ctx = types.SimpleNamespace()
    with pytest.raises(ValueError, match="Unsupported channel_type"):
        await should_react.main(
            ctx, channel_type="ticket", reaction_input=_reaction_input()
        )


@pytest.mark.asyncio
async def test_should_react_chat_allows_bot_message_when_targeted_to_me():
    ctx = types.SimpleNamespace()
    reaction_input = _reaction_input(mentions=["U_ALICE"])
    reaction_input.event.is_bot_message = True
    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )
    assert result["decision"] == "reply"
    assert result["reason"] == "explicit_mention"


@pytest.mark.asyncio
async def test_should_react_chat_replies_to_bot_message_in_participating_thread_without_invoker():
    ctx = types.SimpleNamespace(shared_state={}, pipe="")
    reaction_input = _reaction_input()
    reaction_input.event.is_bot_message = True
    reaction_input.event.is_thread_reply = True
    reaction_input.thread_context.participants.add("alice")
    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )
    assert result["decision"] == "reply"
    assert result["reason"] == "thread_followup"


@pytest.mark.asyncio
async def test_should_react_chat_replies_to_user_message_in_participating_thread():
    ctx = types.SimpleNamespace(shared_state={}, pipe="")
    reaction_input = _reaction_input()
    reaction_input.event.is_thread_reply = True
    reaction_input.thread_context.participants.add("alice")
    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )
    assert result["decision"] == "reply"
    assert result["reason"] == "thread_followup"


@pytest.mark.asyncio
async def test_should_react_chat_uses_llm_decision_for_participating_thread():
    captured: dict[str, object] = {}

    async def invoke(name: str, /, *args, **kwargs):
        captured["name"] = name
        captured["shared_state"] = dict(ctx.shared_state)
        captured["pipe"] = ctx.pipe
        return {"label": "ignore", "reason": "already handled by another agent", "confidence": 0.9}

    ctx = types.SimpleNamespace(shared_state={}, pipe="", invoke=invoke)
    reaction_input = _reaction_input()
    reaction_input.event.is_thread_reply = True
    reaction_input.thread_context.participants.add("alice")

    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )

    assert result["decision"] == "ignore"
    assert result["reason"] == "already handled by another agent"
    assert captured["name"] == "workflows/chat/chat_followup_should_reply"
    assert "chat_should_reply_input" in captured["shared_state"]
    assert captured["shared_state"]["chat_should_reply_input"]["latest_message"]["content"] == "hello"
    assert captured["shared_state"]["chat_should_reply_input"]["is_thread_participant"] is True
    assert "[user_1] hello" in str(captured["pipe"])
    assert ctx.shared_state == {}
    assert ctx.pipe == ""


@pytest.mark.asyncio
async def test_should_react_chat_uses_llm_react_only_for_participating_thread():
    async def invoke(name: str, /, *args, **kwargs):
        return {
            "label": "react_only",
            "reason": "a lightweight acknowledgement is enough",
            "confidence": 0.82,
            "reaction": "ack",
        }

    ctx = types.SimpleNamespace(shared_state={}, pipe="", invoke=invoke)
    reaction_input = _reaction_input()
    reaction_input.event.is_thread_reply = True
    reaction_input.thread_context.participants.add("alice")

    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=reaction_input
    )

    assert result["decision"] == "react_only"
    assert result["reason"] == "a lightweight acknowledgement is enough"
    assert result["reaction"] == "ack"


@pytest.mark.asyncio
async def test_should_react_chat_uses_profile_for_ambient_join_decision():
    captured: dict[str, object] = {}

    async def invoke(name: str, /, *args, **kwargs):
        captured["shared_state"] = dict(ctx.shared_state)
        return {
            "label": "reply",
            "reason": "matches design interests",
            "confidence": 0.84,
        }

    person = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        speaking_style="Concise.",
        relationships="Works with Bob.",
        roles={},
        profile={"character": {"interests": ["UX design"]}},
    )
    ctx = types.SimpleNamespace(person=person, shared_state={}, pipe="", invoke=invoke)

    result = await should_react.main(
        ctx, channel_type="chat", reaction_input=_reaction_input()
    )

    payload = captured["shared_state"]["chat_should_reply_input"]
    assert result["decision"] == "reply"
    assert result["reason"] == "matches design interests"
    assert payload["agent_profile"]["character"]["interests"] == ["UX design"]
    assert payload["is_thread_participant"] is False
