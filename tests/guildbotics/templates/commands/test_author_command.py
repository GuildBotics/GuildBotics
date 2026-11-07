"""The command editor's assistant runs as the bundled command it is.

``assistants/author_command`` is run here through the real
``CommandRunner``, as the environment's entry runs a command, from the
packaged template down to the brain its prompt resolves to, so what reaches
the agent is what the command sends: the message, the resumable
conversation, the working directory.
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
from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import command_access
from guildbotics.commands.runner import CommandRunner
from guildbotics.commands.validation import CommandValidationError
from guildbotics.intelligences.agent_runtime.wire import CommandAccess
from tests.guildbotics.templates.commands.assistant_doubles import (
    AgentContext,
    ScriptedAgent,
)

COMMAND = "assistants/author_command"
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


def _path(brain: ScriptedAgent) -> Path:
    return resolve_named_command(AgentContext(brain), COMMAND)


async def _run(tmp_path: Path, brain: ScriptedAgent, message: dict[str, Any]) -> Any:
    context = AgentContext(brain)
    context.pipe = json.dumps(message, ensure_ascii=False)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    runner = CommandRunner(
        context,
        COMMAND,
        [],
        cwd,
        path=_path(brain),
        mounts={cwd.as_posix(): False},
    )
    return (await runner.run()).result


async def _author(
    tmp_path: Path, *replies: Any, **request: Any
) -> tuple[CommandAuthoringResult, ScriptedAgent]:
    brain = ScriptedAgent(CommandAuthoringResult, *replies)
    result = await _run(tmp_path, brain, _request(**request))
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
    assert turn["execution"] == {"resume_policy": "auto"}
    assert turn["cwd"] == tmp_path / "work"
    # The deleted host read this access via prepare_command; main reads the
    # command's own declaration, which the host binds the run to.
    assert command_access(_path(brain)) == CommandAccess(read_only=True)


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
        await _run(tmp_path, brain, _request())

    assert len(brain.turns) == 2
