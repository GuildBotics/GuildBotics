"""Optional smoke test of GitHub Copilot turns in this device's isolated environment.

Skipped unless ``GUILDBOTICS_COPILOT_SMOKE=1``. Each turn runs the Copilot CLI
pinned in the snapshot with the login saved here, in the microVM of a command
started as the host starts one (see ``conftest.py`` for what that takes). The
turns send minimal prompts, so they consume account quota and never run in
normal CI. Nothing they observe is written to a fixture: prompts, responses,
credentials and session history stay in the run output only.
"""

from __future__ import annotations

import json
import os

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    ResolvedAccess,
    ResolvedGrant,
)
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.intelligences.agent_runtime.copilot import CopilotAcpAdapter
from guildbotics.intelligences.agent_runtime.environment import command_environment
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from tests.guildbotics.intelligences.agent_runtime.contract_doubles import (
    settle_contract,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_COPILOT_SMOKE") != "1",
        reason="Set GUILDBOTICS_COPILOT_SMOKE=1 to run the real Copilot smoke test.",
    ),
    pytest.mark.asyncio,
]

TOOL = "copilot"
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
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    first_events: list[AgentEvent] = []

    async with command_environment(CommandAccess(), frozenset({TOOL})):
        adapter = CopilotAcpAdapter()
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

    # A later command, in a microVM of its own, is what makes the reload real:
    # the session id on the conversation and the state the device keeps for
    # the tool are all the next turn has to go on.
    async with command_environment(CommandAccess(), frozenset({TOOL})):
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
            # Reasoning is left out: it streams in tokens, and this turn's own
            # may well name the word the first one answered.
            assert not [
                event
                for event in second_events
                if event.name != "thinking"
                and event.message.strip() == first.output.strip()
            ]

            # A third turn shares the command's microVM but starts a process of
            # its own, so it reloads the session as the second one did.
            third_events: list[AgentEvent] = []
            third = await adapter.run_turn(
                "Reply with the single word THIRD.",
                context,
                conversation,
                third_events.append,
            )
            _report("third turn (same command)", third_events)
            assert third.output.strip() == "THIRD", third.output
            assert third.provider_session_id == first.provider_session_id
            assert [
                event for event in third_events if event.name == "history_rehydrated"
            ], "the third turn's process must reload the session too"
        finally:
            await adapter.close()


async def test_real_copilot_read_only_turn_cannot_write(tmp_path, monkeypatch) -> None:
    """A read-only turn is held by its environment, not by the prompt: Copilot
    runs as on any turn, and a `read_write` grant is read-only all the same."""
    granted = tmp_path / "granted"
    granted.mkdir()
    settle_contract(
        monkeypatch,
        AccessContract(
            access=ResolvedAccess(
                documents=(ResolvedGrant(granted, "read_write", "granted"),)
            )
        ),
    )
    adapter = CopilotAcpAdapter()
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    async with command_environment(CommandAccess(read_only=True), frozenset({TOOL})):
        try:
            result = await adapter.run_turn(
                f"Create a file named {guest_path(granted)}/smoke.txt containing the "
                "word HELLO, then reply with exactly DONE or FAILED.",
                context,
                conversation,
                events.append,
            )
            _report("read-only turn", events)
            assert _settings(events)["allow_all"] == "on"
            assert not (granted / "smoke.txt").exists(), result.output
        finally:
            await adapter.close()
