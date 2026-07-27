"""Tests for the provider-neutral command-authoring turn contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.authoring import CommandAuthoringResult, author_command_turn
from guildbotics.commands.errors import CommandError

AUTHOR_PROMPT = Path("guildbotics/templates/commands/functions/author_command")


class _BrainStub:
    def __init__(self) -> None:
        self.message = ""
        self.kwargs: dict[str, Any] = {}

    async def run(self, message: str, **kwargs: Any) -> CommandAuthoringResult:
        self.message = message
        self.kwargs = kwargs
        return CommandAuthoringResult(
            message="Draft updated.",
            command="reports/weekly",
            format="python",
            content="def main(context):\n    return 'new'\n",
        )


class _InvalidBrainStub:
    async def run(self, message: str, **kwargs: Any) -> str:
        return "not structured"


class _CorrectionBrainStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def run(self, message: str, **kwargs: Any) -> CommandAuthoringResult:
        self.messages.append(message)
        if len(self.messages) == 1:
            content = "---\nargs:\n  - name: text\n---\nPolish the text.\n"
        else:
            content = (
                "---\nbrain: default\ninputs:\n  message: required\n---\n"
                "Polish the supplied input text.\n"
            )
        return CommandAuthoringResult(
            message="Draft updated.",
            command="polish-email",
            format="markdown",
            content=content,
        )


class _BrainSelectionCorrectionStub:
    def __init__(self, first_brain: str) -> None:
        self.messages: list[str] = []
        self.first_brain = first_brain

    async def run(self, message: str, **kwargs: Any) -> CommandAuthoringResult:
        self.messages.append(message)
        brain = self.first_brain if len(self.messages) == 1 else "brain: default\n"
        return CommandAuthoringResult(
            message="Draft updated.",
            command="polish-email",
            format="markdown",
            content=(
                "---\n"
                f"{brain}"
                "inputs:\n"
                "  message: required\n"
                "---\n"
                "Polish the supplied input text as a business email.\n"
            ),
        )


class _AlwaysInvalidCorrectionBrainStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def run(self, message: str, **kwargs: Any) -> CommandAuthoringResult:
        self.messages.append(message)
        return CommandAuthoringResult(
            message="Draft updated.",
            command="polish-email",
            format="markdown",
            content="---\nbrain: default\nargs:\n  - name: text\n---\nBody.\n",
        )


class _ContextStub:
    def __init__(self, brain: Any) -> None:
        self.brain = brain

    def get_brain(self, name: str, config: None, class_resolver: None) -> _BrainStub:
        assert name == "functions/author_command"
        assert config is None
        assert class_resolver is None
        return self.brain


@pytest.mark.asyncio
async def test_author_command_turn_sends_current_draft_and_stable_conversation(
    tmp_path: Path,
) -> None:
    brain = _BrainStub()

    result = await author_command_turn(
        _ContextStub(brain),
        mode="edit",
        conversation_id="authoring-1",
        trace_id="trace-2",
        command="reports/weekly",
        command_format="python",
        content="def main(context):\n    return 'old'\n",
        instruction="Return the current week.",
        workspace_data_root=tmp_path,
    )

    assert result.content == "def main(context):\n    return 'new'\n"
    assert json.loads(brain.message) == {
        "mode": "edit",
        "command": "reports/weekly",
        "format": "python",
        "current_content": "def main(context):\n    return 'old'\n",
        "instruction": "Return the current week.",
    }
    execution = brain.kwargs["session_state"]["agent_execution_context"]
    assert execution == {
        "run_id": "trace-2",
        "work_kind": "command_authoring",
        "work_identity": "authoring-1",
        "resume_policy": "auto",
        "workspace_data_root": str(tmp_path),
        # Authoring writes command drafts, so it is not a read-only turn.
        "read_only": False,
    }
    assert brain.kwargs["cwd"] == tmp_path / "command-authoring"


@pytest.mark.asyncio
async def test_author_command_turn_rejects_unstructured_agent_output(
    tmp_path: Path,
) -> None:
    with pytest.raises(CommandError, match="structured response"):
        await author_command_turn(
            _ContextStub(_InvalidBrainStub()),
            mode="edit",
            conversation_id="authoring-1",
            trace_id="trace-1",
            command="demo",
            command_format="markdown",
            content="body",
            instruction="Update it",
            workspace_data_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_author_command_turn_allows_ai_to_choose_new_command_identity(
    tmp_path: Path,
) -> None:
    result = await author_command_turn(
        _ContextStub(_BrainStub()),
        mode="create",
        conversation_id="authoring-1",
        trace_id="trace-1",
        command="",
        command_format=None,
        content="",
        instruction="Create a weekly report.",
        workspace_data_root=tmp_path,
    )

    assert result.command == "reports/weekly"
    assert result.format == "python"


@pytest.mark.asyncio
async def test_author_command_turn_rejects_identity_change_for_existing_command(
    tmp_path: Path,
) -> None:
    with pytest.raises(CommandError, match="cannot rename or change the format"):
        await author_command_turn(
            _ContextStub(_BrainStub()),
            mode="edit",
            conversation_id="authoring-1",
            trace_id="trace-1",
            command="existing",
            command_format="markdown",
            content="body",
            instruction="Update it",
            workspace_data_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_author_command_turn_retries_invalid_generated_source(
    tmp_path: Path,
) -> None:
    brain = _CorrectionBrainStub()

    result = await author_command_turn(
        _ContextStub(brain),
        mode="create",
        conversation_id="authoring-1",
        trace_id="trace-1",
        command="",
        command_format=None,
        content="",
        instruction="Polish input email text.",
        workspace_data_root=tmp_path,
    )

    assert len(brain.messages) == 2
    correction = json.loads(brain.messages[1])
    assert correction["validation_error"] == "Command 'args' must be a mapping."
    assert result.content == (
        "---\nbrain: default\ninputs:\n  message: required\n---\n"
        "Polish the supplied input text.\n"
    )


@pytest.mark.asyncio
async def test_author_command_turn_retries_no_op_message_transform(
    tmp_path: Path,
) -> None:
    brain = _BrainSelectionCorrectionStub("brain: none\n")

    result = await author_command_turn(
        _ContextStub(brain),
        mode="create",
        conversation_id="authoring-1",
        trace_id="trace-1",
        command="",
        command_format=None,
        content="",
        instruction="Polish the entered text as a business email.",
        workspace_data_root=tmp_path,
    )

    assert len(brain.messages) == 2
    correction = json.loads(brain.messages[1])
    assert "brain: none" in correction["validation_error"]
    assert "brain: none" not in result.content
    assert "brain: default" in result.content


@pytest.mark.asyncio
async def test_author_command_turn_retries_implicit_default_brain(
    tmp_path: Path,
) -> None:
    brain = _BrainSelectionCorrectionStub("")

    result = await author_command_turn(
        _ContextStub(brain),
        mode="create",
        conversation_id="authoring-1",
        trace_id="trace-1",
        command="",
        command_format=None,
        content="",
        instruction="Polish the entered text as a business email.",
        workspace_data_root=tmp_path,
    )

    assert len(brain.messages) == 2
    correction = json.loads(brain.messages[1])
    assert "explicitly declare 'brain'" in correction["validation_error"]
    assert "brain: default" in result.content


@pytest.mark.asyncio
async def test_author_command_turn_reports_validation_after_failed_retry(
    tmp_path: Path,
) -> None:
    brain = _AlwaysInvalidCorrectionBrainStub()

    result = await author_command_turn(
        _ContextStub(brain),
        mode="create",
        conversation_id="authoring-1",
        trace_id="trace-1",
        command="",
        command_format=None,
        content="",
        instruction="Create a command.",
        workspace_data_root=tmp_path,
    )

    assert len(brain.messages) == 2
    assert "Command 'args' must be a mapping" in result.message


@pytest.mark.parametrize(
    "language",
    ["en", "ja"],
)
def test_author_prompt_uses_message_for_free_form_input(
    language: str,
) -> None:
    body = AUTHOR_PROMPT.with_suffix(f".{language}.md").read_text(encoding="utf-8")

    assert "Context.pipe" in body
    assert "message: required" in body
    assert "args:" in body
    assert "target:" in body
    assert "brain: default" in body
