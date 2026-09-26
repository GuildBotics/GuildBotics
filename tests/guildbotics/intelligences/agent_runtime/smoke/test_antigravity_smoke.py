"""Optional smoke test of Antigravity (``agy``) in this device's isolated environment.

Skipped unless ``GUILDBOTICS_ANTIGRAVITY_SMOKE=1``. Each turn runs the ``agy``
pinned in the snapshot with the login saved here, in the microVM of a command
started as the host starts one, and the usage probe runs where the Desktop runs
it (see ``conftest.py`` for what that takes). The turns send minimal prompts,
so they consume account quota and never run in normal CI. Nothing they observe
is written to a fixture: prompts, responses, credentials and conversation
history stay in the run output only.
"""

from __future__ import annotations

import json
import os

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
)
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.intelligences.agent_runtime.antigravity import (
    AntigravityStreamJsonAdapter,
)
from guildbotics.intelligences.agent_runtime.environment import command_environment
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.usage import (
    _print_output,
    parse_antigravity_usage,
    read_antigravity_usage,
)
from guildbotics.intelligences.cli_agents import ANTIGRAVITY_USAGE_COMMAND
from tests.guildbotics.intelligences.agent_runtime.contract_doubles import (
    settle_contract,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_ANTIGRAVITY_SMOKE") != "1",
        reason=(
            "Set GUILDBOTICS_ANTIGRAVITY_SMOKE=1 to run the real Antigravity "
            "smoke test."
        ),
    ),
    pytest.mark.asyncio,
]

TOOL = "antigravity"
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
        workspace_root=tmp_path,
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
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    first_events: list[AgentEvent] = []

    async with command_environment(CommandAccess(), frozenset({TOOL})):
        adapter = AntigravityStreamJsonAdapter()
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
            assert (
                _named(first_events, "initialized")["model"] == SMOKE_OPTIONS["model"]
            )
            # Token usage arrives on the stream-json `result` event.
            assert first.usage.get("input_tokens", 0) > 0, first.usage
        finally:
            await adapter.close()

    # A later command, in a microVM of its own, is what makes the resume real:
    # the conversation id on the record and the state the device keeps for the
    # tool are all the next turn has to go on.
    async with command_environment(CommandAccess(), frozenset({TOOL})):
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
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    async with command_environment(CommandAccess(), frozenset({TOOL})):
        adapter = AntigravityStreamJsonAdapter()
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


async def test_real_antigravity_read_only_turn_cannot_write_or_reach_out(
    tmp_path, monkeypatch
) -> None:
    """`agy` has no read-only mode of its own; its environment holds the turn.

    ``--mode plan`` still writes under ``--dangerously-skip-permissions`` and
    ``--sandbox`` only confines terminal commands, so what keeps a read-only
    turn from a ``read_write`` grant and from a domain the workspace allows is
    the environment's read-only mounts and closed egress.
    """
    granted = tmp_path / "granted"
    granted.mkdir()
    settle_contract(
        monkeypatch,
        AccessContract(
            network=NetworkPolicy(mode="allowlist", allowed_domains=["example.com"]),
            access=ResolvedAccess(
                documents=(ResolvedGrant(granted, "read_write", "granted"),)
            ),
        ),
    )
    adapter = AntigravityStreamJsonAdapter()
    context = _context(tmp_path)
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    async with command_environment(CommandAccess(read_only=True), frozenset({TOOL})):
        try:
            result = await adapter.run_turn(
                "Run these two shell commands and do not work around a failure: "
                f"`touch {guest_path(granted)}/smoke.txt` and "
                "`curl -sS -o /dev/null -w '%{http_code}' https://example.com`. "
                "Reply with exactly two lines: `WRITE=<ok or failed>` and "
                "`HTTP=<the status code curl printed, or failed>`.",
                context,
                conversation,
                events.append,
            )
            _report("read-only turn", events)
            assert not (granted / "smoke.txt").exists(), result.output
            assert "HTTP=200" not in result.output, result.output
        finally:
            await adapter.close()


async def test_real_antigravity_usage_probe_is_read_only() -> None:
    """`agy -p /usage` must not start a turn, spend quota, or save a conversation."""
    stdout, returncode = await _print_output(
        TOOL, *ANTIGRAVITY_USAGE_COMMAND, timeout=30, label="Antigravity"
    )
    assert returncode == 0, stdout.decode(errors="replace")[:500]
    payload = json.loads(stdout)
    print(
        json.dumps(
            {
                "conversation_id": payload.get("conversation_id"),
                "num_turns": payload.get("num_turns"),
                "usage": payload.get("usage"),
                "status": payload.get("status"),
            },
            ensure_ascii=False,
        )
    )
    assert payload.get("conversation_id") in ("", None)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    assert (usage.get("total_tokens") or 0) == 0
    assert (payload.get("num_turns") or 0) == 0
    assert parse_antigravity_usage(payload).windows
    snapshot = await read_antigravity_usage()
    assert snapshot.agent == "antigravity"
    assert snapshot.windows
