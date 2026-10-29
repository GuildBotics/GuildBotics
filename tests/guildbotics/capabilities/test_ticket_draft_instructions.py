"""Every instruction to draft an issue must preserve the duplicate-check workflow."""

from pathlib import Path

import pytest

from guildbotics.capabilities.member_reference import capability_reference_text

ROOT = Path(__file__).resolve().parents[3]
SOURCES = (
    "member_reference",
    "AGENTS.md",
    ".agents/skills/update-dependencies/SKILL.md",
    ".agents/skills/verify-ai-cli-tools/SKILL.md",
)


@pytest.mark.parametrize("source", SOURCES)
def test_draft_instructions_define_search_and_unavailable_search_outcomes(source):
    text = (
        capability_reference_text()
        if source == "member_reference"
        else (ROOT / source).read_text(encoding="utf-8")
    )
    text = " ".join(text.split())
    for required in (
        "tracker_tickets --subject <tracker> --params",
        '"text":',
        '"state":"all"',
        "continuation",
    ):
        assert required in text, (source, required)

    # One long AND query or a partial read must not silently justify a new issue.
    # A known open match still wins over presenting an unverified draft.
    required_outcomes = (
        (
            "短い語の組を複数",
            "同義語・英語/日本語",
            "語の組ごとに同じ条件",
            "完全な下書き",
            "試した検索条件",
            "重複を否定できなかった",
            "既に開いた同件が見つかっていれば",
        )
        if source == "AGENTS.md"
        else (
            "short term sets",
            "synonyms and English/Japanese variants",
            "identical conditions for each term set",
            "no open match was found",
            "still present the complete draft",
            "conditions tried",
            "duplicates could not be ruled out",
        )
    )
    for required in required_outcomes:
        assert required in text, (source, required)
