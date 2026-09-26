"""Tests for the troubleshooting prompt.

The command that sends it is tested in
``tests/guildbotics/templates/commands/test_assistant_commands.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TROUBLESHOOT_PROMPT = Path("guildbotics/templates/commands/functions/troubleshoot")


@pytest.mark.parametrize("language", ["en", "ja"])
def test_troubleshoot_prompt_teaches_the_read_only_investigation_tools(
    language: str,
) -> None:
    body = TROUBLESHOOT_PROMPT.with_suffix(f".{language}.md").read_text(
        encoding="utf-8"
    )

    # The agent reads the mounted files itself; no CLI exists in its
    # environment to run instead.
    assert "`directories`" in body
    assert "diagnostics.jsonl" in body
    assert "sessions/<trace_id>.jsonl" in body
    assert "sessions/system-*.jsonl" in body
    assert "team/members/<person_id>/commands/" in body
    assert "guildbotics diagnostics" not in body
    # The completion rule the Desktop screens use, stated for the agent.
    assert "command.finished" in body
    # Record structure it has to interpret.
    assert "trace_id" in body
    assert "span_id" in body
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
