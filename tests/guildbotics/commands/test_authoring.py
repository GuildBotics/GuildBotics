"""Tests for the provider-neutral command-authoring turn contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.authoring import (
    CommandAuthoringChange,
    CommandAuthoringFormat,
    CommandAuthoringMode,
    CommandAuthoringResult,
    author_command_turn,
)
from guildbotics.commands.errors import CommandError
from guildbotics.commands.validation import CommandValidationError

AUTHOR_PROMPT = Path("guildbotics/templates/commands/functions/author_command")
PYTHON_SOURCE = "def main(context):\n    return 'new'\n"
CURRENT_SOURCE = "def main(context):\n    return 'old'\n"


def _proposal(*changes: CommandAuthoringChange) -> CommandAuthoringResult:
    return CommandAuthoringResult(
        action="propose_changes",
        message="Review the proposed changes.",
        changes=list(changes),
    )


class _BrainStub:
    def __init__(self, *results: CommandAuthoringResult | str) -> None:
        self.results = list(results)
        self.messages: list[str] = []
        self.kwargs: dict[str, Any] = {}

    async def run(self, message: str, **kwargs: Any) -> CommandAuthoringResult | str:
        self.messages.append(message)
        self.kwargs = kwargs
        index = min(len(self.messages) - 1, len(self.results) - 1)
        return self.results[index]


class _ContextStub:
    def __init__(self, brain: _BrainStub) -> None:
        self.brain = brain

    def get_brain(self, name: str, config: None, class_resolver: None) -> _BrainStub:
        assert name == "functions/author_command"
        assert config is None
        assert class_resolver is None
        return self.brain


async def _run(
    tmp_path: Path,
    brain: _BrainStub,
    *,
    mode: CommandAuthoringMode = "edit",
    command: str = "reports/weekly",
    command_format: CommandAuthoringFormat | None = "python",
    content: str = CURRENT_SOURCE,
    instruction: str = "Return the current week.",
) -> CommandAuthoringResult:
    return await author_command_turn(
        _ContextStub(brain),
        mode=mode,
        conversation_id="authoring-1",
        trace_id="trace-2",
        command=command,
        command_format=command_format,
        content=content,
        instruction=instruction,
        available_commands=[
            {
                "command": "ocr/extract-text",
                "format": "python",
                "relative_path": "ocr/extract-text.py",
                "content": "def main(context):\n    return context.pipe\n",
            }
        ],
        workspace_data_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_author_command_turn_sends_scope_and_uses_read_only_session(
    tmp_path: Path,
) -> None:
    change = CommandAuthoringChange(
        operation="update",
        command="reports/weekly",
        format="python",
        content=PYTHON_SOURCE,
    )
    brain = _BrainStub(_proposal(change))

    result = await _run(tmp_path, brain)

    assert result.changes == [change]
    assert json.loads(brain.messages[0]) == {
        "mode": "edit",
        "command": "reports/weekly",
        "format": "python",
        "current_content": CURRENT_SOURCE,
        "instruction": "Return the current week.",
        "available_commands": [
            {
                "command": "ocr/extract-text",
                "format": "python",
                "relative_path": "ocr/extract-text.py",
                "content": "def main(context):\n    return context.pipe\n",
            }
        ],
        "allowed_operations": {
            "update_current_command": True,
            "create_shared_commands": True,
            "delete_commands": False,
            "change_current_command_format": False,
            "modify_platform_code": False,
        },
    }
    execution = brain.kwargs["session_state"]["agent_execution_context"]
    assert execution == {
        "run_id": "trace-2",
        "work_kind": "command_authoring",
        "work_identity": "authoring-1",
        "resume_policy": "auto",
        "workspace_data_root": str(tmp_path),
        "read_only": True,
    }
    assert brain.kwargs["cwd"] == tmp_path / "command-authoring"


@pytest.mark.asyncio
async def test_question_answer_skips_validation_and_never_proposes_a_change(
    tmp_path: Path,
) -> None:
    current = (
        "---\nname: Translate\nbrain: translation\n"
        "inputs:\n  message: required\n---\nTranslate the input.\n"
    )
    brain = _BrainStub(
        CommandAuthoringResult(
            action="answer",
            message="現在の許可範囲では、新しいPython helperの提案により実現可能です。",
            changes=[],
        )
    )

    result = await _run(
        tmp_path,
        brain,
        command="translate",
        command_format="markdown",
        content=current,
        instruction="とりあえずできるかどうかだけ教えてください。",
    )

    assert result.action == "answer"
    assert result.changes == []
    assert len(brain.messages) == 1
    assert json.loads(brain.messages[0])["current_content"] == current


@pytest.mark.asyncio
async def test_author_command_turn_rejects_unstructured_agent_output(
    tmp_path: Path,
) -> None:
    with pytest.raises(CommandError, match="structured response"):
        await _run(tmp_path, _BrainStub("not structured"))


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

    result = await _run(
        tmp_path,
        _BrainStub(_proposal(primary, helper)),
        mode="create",
        command="",
        command_format=None,
        content="",
        instruction="Create a file-aware translation command.",
    )

    assert result.changes == [primary, helper]


@pytest.mark.asyncio
async def test_invalid_generated_source_is_retried_with_original_instruction(
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
    brain = _BrainStub(_proposal(invalid), _proposal(corrected))

    result = await _run(
        tmp_path,
        brain,
        mode="create",
        command="",
        command_format=None,
        content="",
        instruction="Polish input email text.",
    )

    correction = json.loads(brain.messages[1])
    assert correction["original_instruction"] == "Polish input email text."
    assert correction["validation_error"] == "Command 'args' must be a mapping."
    assert result.changes == [corrected]


@pytest.mark.asyncio
async def test_no_op_proposal_can_be_corrected_to_an_answer(tmp_path: Path) -> None:
    no_op = CommandAuthoringChange(
        operation="update",
        command="reports/weekly",
        format="python",
        content=CURRENT_SOURCE,
    )
    answer = CommandAuthoringResult(
        action="answer", message="No source change was requested.", changes=[]
    )
    brain = _BrainStub(_proposal(no_op), answer)

    result = await _run(tmp_path, brain, instruction="Can this be done?")

    assert result == answer
    assert len(brain.messages) == 2


@pytest.mark.asyncio
async def test_second_invalid_proposal_is_rejected(tmp_path: Path) -> None:
    wrong_target = CommandAuthoringChange(
        operation="update",
        command="another-command",
        format="python",
        content=PYTHON_SOURCE,
    )
    brain = _BrainStub(_proposal(wrong_target))

    with pytest.raises(CommandValidationError, match="currently edited command"):
        await _run(tmp_path, brain)

    assert len(brain.messages) == 2


@pytest.mark.parametrize("language", ["en", "ja"])
def test_author_prompt_defines_answer_and_reviewed_proposal_contract(language: str) -> None:
    body = AUTHOR_PROMPT.with_suffix(f".{language}.md").read_text(encoding="utf-8")

    assert "action: answer" in body
    assert "action: propose_changes" in body
    assert "available_commands" in body
    assert "allowed_operations" in body
    assert "`message`" in body
    assert "Markdown fence" in body
    assert "Context.pipe" in body
    assert "brain: default" in body
