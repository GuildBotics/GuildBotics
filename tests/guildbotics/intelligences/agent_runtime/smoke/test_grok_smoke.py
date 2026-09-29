"""Optional smoke test of Grok Build turns in this device's isolated environment.

Skipped unless ``GUILDBOTICS_GROK_SMOKE=1``. Each turn runs the Grok Build CLI
pinned in the snapshot with the login saved here, in the microVM of a command
started as the host starts one, driven from inside it (``turns.py``; see
``conftest.py`` for what the device takes). The
turns send minimal prompts, so they consume account quota and never run in
normal CI. Nothing they observe is written to a fixture: prompts, responses,
credentials and session history stay in the run output only.
"""

from __future__ import annotations

import json

import pytest

from guildbotics.intelligences.agent_environment.provider_state import (
    provider_state_dir,
)
from guildbotics.intelligences.agent_runtime.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.models import AgentEvent
from guildbotics.intelligences.cli_agents import cli_agent_info
from tests.guildbotics.intelligences.agent_runtime.smoke.turns import run_turns

pytestmark = [
    pytest.mark.real_device("GUILDBOTICS_GROK_SMOKE"),
    pytest.mark.asyncio,
]

TOOL = "grok"
PROMPT = "Reply with the single word OK and nothing else."


def _session_model(session_id: str) -> str:
    sessions = provider_state_dir(cli_agent_info(TOOL)) / "sessions"
    matches = [
        path
        for path in sessions.rglob("signals.json")
        if path.parent.name == session_id
    ]
    assert len(matches) == 1, f"Expected one signals.json for {session_id}"
    return str(json.loads(matches[0].read_text())["primaryModelId"])


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


async def test_real_grok_prompt_then_exact_reload(tmp_path, monkeypatch) -> None:
    ((first, first_events),) = await run_turns(
        monkeypatch, tmp_path, TOOL, GrokAcpAdapter, [PROMPT]
    )
    _report("first turn", first_events)
    assert not isinstance(first, Exception), first
    print("stop/finish:", first.finish_reason, "usage:", first.usage)
    # The reply is the answer stream only: reasoning must not leak into it.
    assert first.output.strip() == "OK", first.output
    assert first.provider_session_id
    assert first.usage["input_tokens"] > 0
    assert first.usage["output_tokens"] > 0
    assert first.model == _session_model(first.provider_session_id)

    # A later command, in a microVM of its own, is what makes the reload real:
    # the session id on the conversation and the state the device keeps for
    # the tool are all the next turn has to go on.
    ((second, second_events),) = await run_turns(
        monkeypatch,
        tmp_path,
        TOOL,
        GrokAcpAdapter,
        ["Reply with the single word AGAIN."],
        session=first.provider_session_id,
    )
    _report("second turn (session/load)", second_events)
    assert not isinstance(second, Exception), second
    assert second.output.strip() == "AGAIN", second.output
    assert second.provider_session_id == first.provider_session_id
    assert second.model == _session_model(second.provider_session_id)
    replayed = [event for event in second_events if event.name == "history_rehydrated"]
    print("rehydration:", [event.details for event in replayed])
    assert replayed, "session/load must report the replay it absorbed"
    unhandled = [
        event.details["unhandled"]
        for event in second_events
        if event.name == "protocol_extensions"
    ]
    print("unhandled extension channels:", unhandled)
    # A channel that carries token usage must be handled, not summarized.
    assert not [key for entry in unhandled for key in entry if "turn_completed" in key]
    # Nothing from the first answer may be re-emitted on the second turn.
    # Reasoning is left out: it streams in tokens, and this turn's own
    # may well name the word the first one answered.
    assert not [
        event
        for event in second_events
        if event.name != "thinking" and event.message.strip() == first.output.strip()
    ]

    ((selected, selected_events),) = await run_turns(
        monkeypatch,
        tmp_path,
        TOOL,
        GrokAcpAdapter,
        [PROMPT],
        options={"model": first.model},
    )
    _report("turn with an explicit model", selected_events)
    assert not isinstance(selected, Exception), selected
    assert selected.output.strip() == "OK", selected.output
    assert selected.model == _session_model(selected.provider_session_id)
