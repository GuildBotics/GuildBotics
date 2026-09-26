"""Tests for the command-authoring result and prompt.

The command that sends it, and its proposal validation, are tested in
``tests/guildbotics/templates/commands/test_assistant_commands.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.commands.authoring import (
    CommandAuthoringChange,
    CommandAuthoringResult,
)

AUTHOR_PROMPT = Path("guildbotics/templates/commands/functions/author_command")


def test_an_answer_cannot_include_changes() -> None:
    change = CommandAuthoringChange(
        operation="create",
        command="helper",
        format="python",
        content="def main(context):\n    return 'new'\n",
    )

    with pytest.raises(ValidationError):
        CommandAuthoringResult(action="answer", message="Answer.", changes=[change])


def test_a_change_proposal_must_include_a_change() -> None:
    with pytest.raises(ValidationError):
        CommandAuthoringResult(action="propose_changes", message="Review.")


@pytest.mark.parametrize("language", ["en", "ja"])
def test_author_prompt_defines_answer_and_reviewed_proposal_contract(
    language: str,
) -> None:
    body = AUTHOR_PROMPT.with_suffix(f".{language}.md").read_text(encoding="utf-8")

    assert "action: answer" in body
    assert "action: propose_changes" in body
    assert "available_commands" in body
    assert "allowed_operations" in body
    assert "`message`" in body
    assert "Markdown fence" in body
    assert "Context.pipe" in body
    assert "brain: default" in body
