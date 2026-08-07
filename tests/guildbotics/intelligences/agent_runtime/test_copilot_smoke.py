"""Optional smoke test against a real, logged-in GitHub Copilot CLI install.

Skipped unless ``GUILDBOTICS_COPILOT_SMOKE=1``. It sends two minimal prompts, so
it consumes account quota and never runs in normal CI. Nothing it observes is
written to a fixture: prompts, responses, credentials and session history stay
in the run output only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil

import pytest

from guildbotics.intelligences.agent_runtime.copilot import CopilotAcpAdapter
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_COPILOT_SMOKE") != "1",
        reason="Set GUILDBOTICS_COPILOT_SMOKE=1 to run the real Copilot smoke test.",
    ),
    pytest.mark.skipif(
        shutil.which("copilot") is None, reason="GitHub Copilot CLI is not installed."
    ),
    pytest.mark.asyncio,
]

PROMPT = "Reply with the single word OK and nothing else."
#: The cheapest model this account offers, so the smoke costs as little as it
#: can while still proving the model option is really applied.
SMOKE_OPTIONS = {"model": "gpt-5-mini", "reasoning_effort": "low"}


def _context(tmp_path) -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="smoke",
        run_id="smoke-run",
        cwd=tmp_path,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("smoke", "copilot", "manual", "smoke"),
        resume_policy=ResumePolicy.AUTO,
        provider_options=dict(SMOKE_OPTIONS),
    )


def _report(title: str, events: list[AgentEvent]) -> None:
    print(f"\n=== {title} ===")
    for event in events:
        print(
            json.dumps(
                {
                    "kind": event.kind.value,
                    "name": event.name,
                    "message": event.message[:200],
                    "usage": event.usage,
                    "details": event.details,
                },
                ensure_ascii=False,
            )
        )


def _settings(events: list[AgentEvent]) -> dict:
    return next(event for event in events if event.name == "settings").details


async def test_real_copilot_prompt_then_exact_reload(tmp_path) -> None:
    adapter = CopilotAcpAdapter()
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    first_events: list[AgentEvent] = []

    try:
        first = await adapter.run_turn(
            PROMPT, context, conversation, first_events.append
        )
        _report("first turn", first_events)
        print("stop/finish:", first.finish_reason, "usage:", first.usage)
        # The reply is the answer stream only: reasoning must not leak into it.
        assert first.output.strip() == "OK", first.output
        assert first.provider_session_id
        # The effective settings come from Copilot's own answer, not from the
        # request, and the approval policy must really be on for a normal turn.
        assert _settings(first_events) == {
            "model": "gpt-5-mini",
            "reasoning_effort": "low",
            "allow_all": "on",
            "requested": {**SMOKE_OPTIONS, "allow_all": "on"},
            "rejected": [],
        }

    finally:
        await adapter.close()

    # A restarted process is what makes the reload real: the session id on the
    # conversation is all the next turn has to go on.
    adapter = CopilotAcpAdapter()
    try:
        conversation.provider_session_id = first.provider_session_id
        second_events: list[AgentEvent] = []
        second = await adapter.run_turn(
            "Reply with the single word AGAIN.",
            context,
            conversation,
            second_events.append,
        )
        _report("second turn (session/load)", second_events)
        assert second.output.strip() == "AGAIN", second.output
        assert second.provider_session_id == first.provider_session_id
        replayed = [
            event for event in second_events if event.name == "history_rehydrated"
        ]
        print("rehydration:", [event.details for event in replayed])
        assert replayed, "session/load must report the replay it absorbed"
        # A reloaded session keeps its settings, so nothing has to be re-sent.
        assert _settings(second_events)["reasoning_effort"] == "low"
        unhandled = [
            event.details["unhandled"]
            for event in second_events
            if event.name == "protocol_extensions"
        ]
        print("unhandled extension channels:", unhandled)
        # Nothing from the first answer may be re-emitted on the second turn.
        assert not [
            event
            for event in second_events
            if event.message and event.message.strip() == first.output.strip()
        ]

        # A third turn continues the session this process now holds. Copilot
        # refuses to load a session it already has, so nothing may be reloaded.
        third_events: list[AgentEvent] = []
        third = await adapter.run_turn(
            "Reply with the single word THIRD.",
            context,
            conversation,
            third_events.append,
        )
        _report("third turn (same process)", third_events)
        assert third.output.strip() == "THIRD", third.output
        assert not [
            event for event in third_events if event.name == "history_rehydrated"
        ]
    finally:
        await adapter.close()


async def test_real_copilot_read_only_turn_cannot_write(tmp_path) -> None:
    """A read-only turn must be refused at the provider, not by the prompt."""
    adapter = CopilotAcpAdapter()
    context = dataclasses.replace(_context(tmp_path), read_only=True)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    try:
        result = await adapter.run_turn(
            "Create a file named smoke.txt containing the word HELLO in the "
            "current directory, then reply with exactly DONE.",
            context,
            conversation,
            events.append,
        )
        _report("read-only turn", events)
        assert _settings(events)["allow_all"] == "off"
        declined = [event for event in events if event.name == "decision"]
        print("declined tool calls:", [event.details for event in declined])
        assert declined, "a write must have been asked for and refused"
        assert not (tmp_path / "smoke.txt").exists(), result.output
    finally:
        await adapter.close()
