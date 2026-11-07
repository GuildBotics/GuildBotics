"""Diagnostics use the real bundled command runner with a scripted brain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import command_access
from guildbotics.commands.runner import CommandRunner
from guildbotics.guest.entry import _failure
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.intelligences.agent_runtime.wire import CommandAccess
from guildbotics.intelligences.common import MessageResponse
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from tests.guildbotics.templates.commands.assistant_doubles import (
    AgentContext,
    ScriptedAgent,
)


async def run(tmp_path: Path, command: str, brain: ScriptedAgent, message: str) -> Any:
    context = AgentContext(brain)
    context.pipe = message
    path = resolve_named_command(context, command)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    runner = CommandRunner(
        context, command, [], cwd, path=path, mounts={cwd.as_posix(): False}
    )
    return (await runner.run()).result


@pytest.mark.asyncio
async def test_troubleshooting_is_one_read_only_resumable_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))
    answer = TroubleshootingResult(message="The token expired.", trace_ids=["abc123"])
    brain = ScriptedAgent(TroubleshootingResult, answer)
    message = {
        "question": "Why did this fail?",
        "focus": {"view": "trace", "trace_id": "abc123"},
        "directories": {"diagnostics": "/workspace/.guildbotics/local/run"},
    }
    assert (
        await run(tmp_path, "assistants/troubleshoot", brain, json.dumps(message))
        == answer
    )
    (turn,) = brain.turns
    assert json.loads(turn["message"]) == message
    assert turn["execution"] == {"resume_policy": "auto"}
    assert turn["cwd"] == tmp_path / "work"
    path = resolve_named_command(AgentContext(brain), "assistants/troubleshoot")
    assert command_access(path) == CommandAccess(
        read_only=True, inspects=frozenset({"diagnostics", "config"})
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_cli_check_is_one_read_only_turn_and_preserves_tool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))
    refused = CliAgentExecutionResult(stdout="", stderr="login required", returncode=2)
    brain = ScriptedAgent(
        None,
        CliAgentExecutionError(cli_agent="codex", result=refused) if fails else "OK",
    )
    if fails:
        with pytest.raises(CommandError) as error:
            await run(
                tmp_path, "diagnostics/cli_agent", brain, "Reply with exactly OK."
            )
        failure = _failure(error.value)
        assert failure.cli_agent_result is not None
        assert failure.cli_agent_result["returncode"] == 2
        assert failure.cli_agent_result["stderr"] == "login required"
    else:
        assert (
            await run(
                tmp_path, "diagnostics/cli_agent", brain, "Reply with exactly OK."
            )
            == "OK"
        )
    (turn,) = brain.turns
    assert turn["message"] == "Reply with exactly OK."
    assert command_access(
        resolve_named_command(AgentContext(brain), "diagnostics/cli_agent")
    ) == CommandAccess(read_only=True)


@pytest.mark.asyncio
async def test_llm_command_asks_the_members_model_for_one_minimal_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))
    brain = ScriptedAgent(
        MessageResponse, MessageResponse(content=" OK ", author="bot", author_type="AI")
    )
    calls: list[dict[str, Any]] = []
    original_run = brain.run

    async def capture(message: str, **kwargs: Any) -> Any:
        calls.append(kwargs["session_state"])
        return await original_run(message, **kwargs)

    monkeypatch.setattr(brain, "run", capture)
    assert await run(tmp_path, "diagnostics/llm", brain, "ignored") == "OK"
    assert len(brain.turns) == 1
    assert calls[0]["topic"] == "Reply with exactly OK."
    assert calls[0]["context_location"] == "diagnostics"
    assert json.loads(calls[0]["conversation_history"]) == [
        {"content": "Reply with exactly OK.", "author": "User", "author_type": "User"}
    ]
    assert command_access(
        resolve_named_command(AgentContext(brain), "diagnostics/llm")
    ) == CommandAccess(read_only=True)
