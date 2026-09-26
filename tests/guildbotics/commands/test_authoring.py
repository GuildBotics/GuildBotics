"""Tests for the command-authoring prompt.

The command that sends it, and its proposal validation, are tested in
``tests/guildbotics/templates/commands/test_assistant_commands.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AUTHOR_PROMPT = Path("guildbotics/templates/commands/functions/author_command")


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
