"""Optional smoke test against a real, logged-in Antigravity (``agy``) install.

Skipped unless ``GUILDBOTICS_ANTIGRAVITY_SMOKE=1``. It sends minimal prompts, so
it consumes account quota and never runs in normal CI. Nothing it observes is
written to a fixture: prompts, responses, credentials and conversation history
stay in the run output only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil

import pytest

from guildbotics.intelligences.agent_runtime.antigravity import (
    AntigravityStreamJsonAdapter,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_ANTIGRAVITY_SMOKE") != "1",
        reason=(
            "Set GUILDBOTICS_ANTIGRAVITY_SMOKE=1 to run the real Antigravity "
            "smoke test."
        ),
    ),
    pytest.mark.skipif(
        shutil.which("agy") is None, reason="Antigravity CLI is not installed."
    ),
    pytest.mark.asyncio,
]

PROMPT = "Reply with the single word OK and nothing else."
#: The cheapest model this account offers, so the smoke costs as little as it
#: can while still proving the model option is really applied. `--effort` is
#: deliberately absent: `agy` refuses it alongside an explicit model.
SMOKE_OPTIONS = {"model": "gemini-3.6-flash-low"}


def _context(tmp_path) -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="smoke",
        run_id="smoke-run",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("smoke", "antigravity", "manual", "smoke"),
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


def _named(events: list[AgentEvent], name: str) -> dict:
    return next(event for event in events if event.name == name).details


async def test_real_antigravity_prompt_then_exact_resume(tmp_path) -> None:
    adapter = AntigravityStreamJsonAdapter()
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    first_events: list[AgentEvent] = []

    try:
        first = await adapter.run_turn(
            PROMPT, context, conversation, first_events.append
        )
        _report("first turn", first_events)
        print("finish:", first.finish_reason, "usage:", first.usage)
        assert first.output.strip() == "OK", first.output
        assert first.provider_session_id
        assert _named(first_events, "settings")["model"] == SMOKE_OPTIONS["model"]
        # `init` reports the model back only because it was named explicitly.
        assert _named(first_events, "initialized")["model"] == SMOKE_OPTIONS["model"]
        # Usage is what the script path could never report.
        assert first.usage.get("input_tokens", 0) > 0, first.usage
    finally:
        await adapter.close()

    # A new adapter is what makes the resume real: the conversation id on the
    # record is all the next turn has to go on.
    adapter = AntigravityStreamJsonAdapter()
    try:
        conversation.provider_session_id = first.provider_session_id
        second_events: list[AgentEvent] = []
        second = await adapter.run_turn(
            "Reply with the single word AGAIN and nothing else.",
            context,
            conversation,
            second_events.append,
        )
        _report("second turn (--conversation)", second_events)
        assert second.output.strip() == "AGAIN", second.output
        assert second.provider_session_id == first.provider_session_id
        # Nothing from the first answer may be replayed on the second turn.
        assert not [
            event
            for event in second_events
            if event.message and event.message.strip() == first.output.strip()
        ]
    finally:
        await adapter.close()


async def test_real_antigravity_reaches_the_working_directory(tmp_path) -> None:
    """Tools must act in the member's workspace, not in the CLI's own scratch."""
    (tmp_path / "marker.txt").write_text("guildbotics-marker\n")
    adapter = AntigravityStreamJsonAdapter()
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    try:
        result = await adapter.run_turn(
            "Read the file marker.txt in the current directory and reply with "
            "its contents only.",
            context,
            conversation,
            events.append,
        )
        _report("workspace turn", events)
        assert "guildbotics-marker" in result.output, result.output
    finally:
        await adapter.close()


async def test_real_antigravity_read_only_turn_is_recorded_as_unenforced(
    tmp_path,
) -> None:
    """`agy` has no provider-side read-only mode; the gap must stay visible.

    ``--mode plan`` still writes under ``--dangerously-skip-permissions``,
    ``--sandbox`` only confines terminal commands (its own file tools reach
    outside the workspace), and dropping the permission skip makes headless mode
    auto-deny every command and return an empty response. Until ``agy`` grows a
    real one, this pins the honest record rather than a false guarantee.
    """
    adapter = AntigravityStreamJsonAdapter()
    context = dataclasses.replace(_context(tmp_path), read_only=True)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    try:
        result = await adapter.run_turn(
            "Reply with the single word READONLY and nothing else.",
            context,
            conversation,
            events.append,
        )
        _report("read-only turn", events)
        assert _named(events, "policy") == {
            "read_only": True,
            "read_only_enforced": False,
        }
        assert result.output.strip() == "READONLY", result.output
    finally:
        await adapter.close()
