"""The Desktop assistants and the diagnostics check run as bundled commands.

Each is run here through the real ``CommandRunner``, from the packaged
template down to the brain its prompt resolves to, so what reaches the agent
is what the command sends: the message, the resumable conversation, the
working directory, and the access every turn of the run is held to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.authoring import (
    CommandAuthoringChange,
    CommandAuthoringResult,
)
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import CommandAccess
from guildbotics.commands.runner import CommandRunner
from guildbotics.commands.validation import CommandValidationError
from guildbotics.drivers.command_runner import run_in_environment
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from tests.guildbotics.templates.commands.assistant_doubles import (
    AgentContext,
    ScriptedAgent,
)

PYTHON_SOURCE = "def main(context):\n    return 'new'\n"
CURRENT_SOURCE = "def main(context):\n    return 'old'\n"
AVAILABLE = [
    {
        "command": "ocr/extract-text",
        "format": "python",
        "relative_path": "ocr/extract-text.py",
        "content": "def main(context):\n    return context.pipe\n",
    }
]


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))


def _context(agent: ScriptedAgent, message: dict[str, Any]) -> AgentContext:
    context = AgentContext(agent)
    context.pipe = json.dumps(message, ensure_ascii=False)
    return context


async def _run(
    tmp_path: Path, command: str, brain: ScriptedAgent, message: dict[str, Any]
) -> Any:
    runner = CommandRunner(
        _context(brain, message),
        command,
        ["conversation_id=conv-1"],
        cwd=tmp_path / "work",
    )
    return (await run_in_environment(runner)).result


# ---------------------------------------------------------------------------
# troubleshooting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_troubleshooting_is_one_read_only_turn_that_inspects_the_workspace(
    tmp_path: Path,
) -> None:
    answer = TroubleshootingResult(message="The token expired.", trace_ids=["abc123"])
    brain = ScriptedAgent(TroubleshootingResult, answer)
    message = {
        "question": "Why did this fail?",
        "focus": {"view": "trace", "trace_id": "abc123"},
        "directories": {"diagnostics": "/workspace/.guildbotics/local/run"},
    }

    result = await _run(tmp_path, "assistants/troubleshoot", brain, message)

    assert result == answer
    (turn,) = brain.turns
    # The question and where to look reach the agent as the caller sent them.
    assert json.loads(turn["message"]) == message
    # Every turn of the conversation resumes the provider's own session.
    assert turn["execution"] == {
        "work_kind": "troubleshooting",
        "work_identity": "conv-1",
        "resume_policy": "auto",
    }
    assert turn["cwd"] == tmp_path / "work"
    # It changes nothing, and reads the recorded runs and what they ran with.
    assert turn["access"] == CommandAccess(
        read_only=True, inspects=frozenset({"diagnostics", "config"})
    )


# ---------------------------------------------------------------------------
# command authoring
# ---------------------------------------------------------------------------


def _request(**overrides: Any) -> dict[str, Any]:
    return {
        "mode": "edit",
        "command": "reports/weekly",
        "format": "python",
        "current_content": CURRENT_SOURCE,
        "instruction": "Return the current week.",
        "available_commands": AVAILABLE,
        **overrides,
    }


_CREATE = {"mode": "create", "command": "", "format": None, "current_content": ""}


def _proposal(*changes: CommandAuthoringChange) -> CommandAuthoringResult:
    return CommandAuthoringResult(
        action="propose_changes",
        message="Review the proposed changes.",
        changes=list(changes),
    )


async def _author(
    tmp_path: Path, *replies: Any, **request: Any
) -> tuple[CommandAuthoringResult, ScriptedAgent]:
    brain = ScriptedAgent(CommandAuthoringResult, *replies)
    result = await _run(
        tmp_path, "assistants/author_command", brain, _request(**request)
    )
    return result, brain


@pytest.mark.asyncio
async def test_authoring_sends_its_scope_in_one_read_only_turn(tmp_path: Path) -> None:
    change = CommandAuthoringChange(
        operation="update",
        command="reports/weekly",
        format="python",
        content=PYTHON_SOURCE,
    )

    result, brain = await _author(tmp_path, _proposal(change))

    assert result.changes == [change]
    (turn,) = brain.turns
    assert json.loads(turn["message"]) == {
        **_request(),
        "allowed_operations": {
            "update_current_command": True,
            "create_shared_commands": True,
            "delete_commands": False,
            "change_current_command_format": False,
            "modify_platform_code": False,
        },
    }
    assert turn["execution"] == {
        "work_kind": "command_authoring",
        "work_identity": "conv-1",
        "resume_policy": "auto",
    }
    assert turn["cwd"] == tmp_path / "work"
    assert turn["access"] == CommandAccess(read_only=True)


@pytest.mark.asyncio
async def test_an_answer_skips_validation_and_proposes_nothing(tmp_path: Path) -> None:
    answer = CommandAuthoringResult(
        action="answer",
        message="現在の許可範囲では、新しいPython helperの提案により実現可能です。",
        changes=[],
    )

    result, brain = await _author(
        tmp_path,
        answer,
        instruction="とりあえずできるかどうかだけ教えてください。",
    )

    assert result == answer
    assert len(brain.turns) == 1


@pytest.mark.asyncio
async def test_unstructured_agent_output_fails_the_command(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="structured response"):
        await _author(tmp_path, "not structured")


@pytest.mark.asyncio
async def test_create_may_propose_primary_and_helper_commands(tmp_path: Path) -> None:
    primary = CommandAuthoringChange(
        operation="create",
        command="translate-file-aware",
        format="python",
        content=PYTHON_SOURCE,
    )
    helper = CommandAuthoringChange(
        operation="create",
        command="helpers/find-existing-path",
        format="python",
        content="def main(context):\n    return context.pipe\n",
    )

    result, _ = await _author(tmp_path, _proposal(primary, helper), **_CREATE)

    assert result.changes == [primary, helper]


@pytest.mark.asyncio
async def test_an_invalid_proposal_is_sent_back_once_with_the_instruction(
    tmp_path: Path,
) -> None:
    invalid = CommandAuthoringChange(
        operation="create",
        command="polish-email",
        format="markdown",
        content="---\nargs:\n  - name: text\n---\nPolish the text.\n",
    )
    corrected = invalid.model_copy(
        update={
            "content": (
                "---\nbrain: default\ninputs:\n  message: required\n---\n"
                "Polish the supplied input text.\n"
            )
        }
    )

    result, brain = await _author(
        tmp_path,
        _proposal(invalid),
        _proposal(corrected),
        **_CREATE,
        instruction="Polish input email text.",
    )

    correction = json.loads(brain.turns[1]["message"])
    assert correction["original_instruction"] == "Polish input email text."
    assert correction["validation_error"] == "Command 'args' must be a mapping."
    # The correction continues the same conversation.
    assert brain.turns[1]["execution"] == brain.turns[0]["execution"]
    assert result.changes == [corrected]


@pytest.mark.asyncio
async def test_a_no_op_proposal_can_be_corrected_to_an_answer(tmp_path: Path) -> None:
    no_op = CommandAuthoringChange(
        operation="update",
        command="reports/weekly",
        format="python",
        content=CURRENT_SOURCE,
    )
    answer = CommandAuthoringResult(
        action="answer", message="No source change was requested.", changes=[]
    )

    result, brain = await _author(
        tmp_path, _proposal(no_op), answer, instruction="Can this be done?"
    )

    assert result == answer
    assert len(brain.turns) == 2


@pytest.mark.asyncio
async def test_a_second_invalid_proposal_fails_the_command(tmp_path: Path) -> None:
    wrong_target = CommandAuthoringChange(
        operation="update",
        command="another-command",
        format="python",
        content=PYTHON_SOURCE,
    )

    brain = ScriptedAgent(CommandAuthoringResult, _proposal(wrong_target))

    with pytest.raises(CommandValidationError, match="currently edited command"):
        await _run(tmp_path, "assistants/author_command", brain, _request())

    assert len(brain.turns) == 2


# ---------------------------------------------------------------------------
# the diagnostics screen's AI CLI tool check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_cli_agent_check_is_one_read_only_turn_of_its_command(
    tmp_path: Path, fails: bool
) -> None:
    """What the tool did reaches the check through the command that ran it."""
    from guildbotics.app_api.diagnostics import _run_cli_agent_check
    from guildbotics.intelligences.brains.cli_agent import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    refused = CliAgentExecutionResult(stdout="", stderr="login required", returncode=2)
    agent = ScriptedAgent(
        None,
        CliAgentExecutionError(cli_agent="codex", result=refused) if fails else "OK",
    )
    context = AgentContext(agent)
    context.pipe = "Reply with exactly OK."

    result = await _run_cli_agent_check(context, context.person, str(tmp_path))

    assert result == (
        refused
        if fails
        else CliAgentExecutionResult(stdout="OK", stderr="", returncode=0)
    )
    (turn,) = agent.turns
    assert turn["message"] == "Reply with exactly OK."
    assert turn["access"] == CommandAccess(read_only=True)
