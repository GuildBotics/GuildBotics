"""Tests for the troubleshooting turn contract and its prompt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.assistants import AssistantResponseError
from guildbotics.intelligences.troubleshooting import (
    TroubleshootingResult,
    troubleshoot_turn,
)

TROUBLESHOOT_PROMPT = Path("guildbotics/templates/commands/functions/troubleshoot")


class _BrainStub:
    def __init__(self, reply: Any | None = None) -> None:
        self.reply = reply or TroubleshootingResult(
            message="The push failed because the GitHub token expired.",
            trace_ids=["abc123"],
        )
        self.message = ""
        self.kwargs: dict[str, Any] = {}

    async def run(self, message: str, **kwargs: Any) -> Any:
        self.message = message
        self.kwargs = kwargs
        return self.reply


class _ContextStub:
    def __init__(self, brain: Any) -> None:
        self.brain = brain

    def get_brain(self, name: str, config: None, class_resolver: None) -> Any:
        assert name == "functions/troubleshoot"
        return self.brain


@pytest.mark.asyncio
async def test_troubleshoot_turn_sends_question_and_focus(tmp_path: Path) -> None:
    brain = _BrainStub()

    result = await troubleshoot_turn(
        _ContextStub(brain),
        conversation_id="conv-1",
        trace_id="trace-1",
        question="Why did this fail?",
        focus={"view": "trace", "trace_id": "abc123", "source": "routine"},
        workspace_data_root=tmp_path,
    )

    assert result.message.startswith("The push failed")
    assert result.trace_ids == ["abc123"]
    assert json.loads(brain.message) == {
        "question": "Why did this fail?",
        "focus": {"view": "trace", "trace_id": "abc123", "source": "routine"},
    }
    state = brain.kwargs["session_state"]["agent_execution_context"]
    assert state["work_kind"] == "troubleshooting"
    assert state["work_identity"] == "conv-1"
    assert state["run_id"] == "trace-1"
    assert brain.kwargs["cwd"] == tmp_path / "troubleshooting"


@pytest.mark.asyncio
async def test_troubleshoot_turn_rejects_unstructured_output(tmp_path: Path) -> None:
    with pytest.raises(AssistantResponseError):
        await troubleshoot_turn(
            _ContextStub(_BrainStub("not structured")),
            conversation_id="conv-1",
            trace_id="trace-1",
            question="Why?",
            focus={},
            workspace_data_root=tmp_path,
        )


@pytest.mark.parametrize("language", ["en", "ja"])
def test_troubleshoot_prompt_teaches_the_read_only_investigation_tools(
    language: str,
) -> None:
    body = TROUBLESHOOT_PROMPT.with_suffix(f".{language}.md").read_text(
        encoding="utf-8"
    )

    # The agent gathers evidence itself; these are the only tools it may use.
    assert "guildbotics diagnostics traces" in body
    assert "guildbotics diagnostics trace <trace_id>" in body
    assert "guildbotics diagnostics system" in body
    # Record structure it has to interpret.
    assert "trace_id" in body
    assert "span_id" in body
    assert "diagnostics.jsonl" in body
    assert "sessions/<trace_id>.jsonl" in body
    # Guardrails.
    assert "guildbotics member" in body
    assert "troubleshooting" in body
    assert "response_class: guildbotics.intelligences.troubleshooting" in body


def test_troubleshoot_prompt_languages_share_one_structure() -> None:
    headings = {
        language: [
            line
            for line in TROUBLESHOOT_PROMPT.with_suffix(f".{language}.md")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.startswith("## ")
        ]
        for language in ("en", "ja")
    }

    assert len(headings["en"]) == len(headings["ja"]) > 0
