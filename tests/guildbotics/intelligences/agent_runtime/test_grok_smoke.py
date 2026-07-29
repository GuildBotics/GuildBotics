"""Optional smoke test against a real, logged-in Grok Build install.

Skipped unless ``GUILDBOTICS_GROK_SMOKE=1``. It sends one minimal prompt, so it
consumes account quota and never runs in normal CI. Nothing it observes is
written to a fixture: prompts, responses, credentials and session history stay
in the run output only.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from guildbotics.intelligences.agent_runtime.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_GROK_SMOKE") != "1",
        reason="Set GUILDBOTICS_GROK_SMOKE=1 to run the real Grok Build smoke test.",
    ),
    pytest.mark.skipif(
        shutil.which("grok") is None, reason="Grok Build is not installed."
    ),
    pytest.mark.asyncio,
]

PROMPT = "Reply with the single word OK and nothing else."


def _context(tmp_path) -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="smoke",
        run_id="smoke-run",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("smoke", "grok", "manual", "smoke"),
        resume_policy=ResumePolicy.AUTO,
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


async def test_real_grok_prompt_then_exact_reload(tmp_path) -> None:
    adapter = GrokAcpAdapter()
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
        assert first.usage["input_tokens"] > 0
        assert first.usage["output_tokens"] > 0

        # The second turn must reload that exact session, not the latest one.
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
        unhandled = [
            event.details["unhandled"]
            for event in second_events
            if event.name == "protocol_extensions"
        ]
        print("unhandled extension channels:", unhandled)
        # A channel that carries token usage must be handled, not summarized.
        assert not [
            key for entry in unhandled for key in entry if "turn_completed" in key
        ]
        # Nothing from the first answer may be re-emitted on the second turn.
        assert not [
            event
            for event in second_events
            if event.message and event.message.strip() == first.output.strip()
        ]
    finally:
        await adapter.close()
