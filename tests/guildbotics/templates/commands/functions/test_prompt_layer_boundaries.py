"""Layer-boundary tests for the member prompt sources.

Prompt layer model (see docs/spec/member_prompt_layering_plan.ja.md):
- mode-independent knowledge → member capability reference (member context/help)
- shared workflow envelope → i18n ``workflow_contract`` injected as
  ``{workflow_contract}``
- trigger-specific contract → ``handle_github_ticket`` / ``handle_chat_event``
- interactive envelope → ``skills/guildbotics/SKILL.md``

These tests keep the boundaries mechanical: shared wording must not creep back
into individual prompts, and the en/ja prompt variants must not drift apart.
"""

import re
from pathlib import Path

from guildbotics.utils.fileio import load_markdown_with_frontmatter

FUNCTIONS_DIR = Path("guildbotics/templates/commands/functions")
SKILL_PATH = Path("skills/guildbotics/SKILL.md")
WORKFLOW_PROMPTS = ("handle_github_ticket", "handle_chat_event")

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_MEMBER_COMMAND_RE = re.compile(r"guildbotics member ([a-z]+(?: [a-z]+)*)")


def _prompt_body(name: str, language: str) -> str:
    prompt = load_markdown_with_frontmatter(FUNCTIONS_DIR / f"{name}.{language}.md")
    return prompt["body"]


def _instruction_step_count(body: str) -> int:
    section = body.split("<instructions>")[1].split("</instructions>")[0]
    return len(re.findall(r"^\d+\.", section, flags=re.MULTILINE))


def test_prompt_placeholders_match_across_languages():
    for name in WORKFLOW_PROMPTS:
        english = set(_PLACEHOLDER_RE.findall(_prompt_body(name, "en")))
        japanese = set(_PLACEHOLDER_RE.findall(_prompt_body(name, "ja")))
        assert english == japanese, name


def test_prompt_member_commands_match_across_languages():
    for name in WORKFLOW_PROMPTS:
        english = set(_MEMBER_COMMAND_RE.findall(_prompt_body(name, "en")))
        japanese = set(_MEMBER_COMMAND_RE.findall(_prompt_body(name, "ja")))
        assert english == japanese, name


def test_prompt_instruction_step_counts_match_across_languages():
    for name in WORKFLOW_PROMPTS:
        english = _instruction_step_count(_prompt_body(name, "en"))
        japanese = _instruction_step_count(_prompt_body(name, "ja"))
        assert english == japanese, name


def test_workflow_prompts_inject_envelope_instead_of_restating_it():
    for name in WORKFLOW_PROMPTS:
        for language in ("en", "ja"):
            body = _prompt_body(name, language)
            assert "{workflow_contract}" in body, (name, language)
            # Envelope rules live in the injected contract only.
            assert "--workspace-mode" not in body, (name, language)
            assert "guildbotics member context" not in body, (name, language)


def test_prompts_do_not_restate_member_reference_contracts():
    # Sentinel phrases owned by the member capability reference: the PR
    # work-record memory contract and the communication-style mapping.
    sentinels = (
        "remaining follow-up",
        "残 follow-up",
        "neutral_documents",
        "machine_outputs",
        "memory record --pr",
    )
    bodies = {
        (name, language): _prompt_body(name, language)
        for name in WORKFLOW_PROMPTS
        for language in ("en", "ja")
    }
    bodies[("skill", "en")] = load_markdown_with_frontmatter(SKILL_PATH)["body"]
    for key, body in bodies.items():
        for sentinel in sentinels:
            assert sentinel not in body, (key, sentinel)


def test_skill_excludes_workflow_run_contract():
    body = load_markdown_with_frontmatter(SKILL_PATH)["body"]
    # Run-completion commands are workflow-only; the interactive envelope must
    # not mention them beyond the marker guardrail.
    assert "member task complete" not in body
    assert "member chat complete" not in body
    assert "--workspace-mode member" not in body


def test_chat_prompt_supports_code_work_without_an_issue():
    english = _prompt_body("handle_chat_event", "en")
    japanese = _prompt_body("handle_chat_event", "ja")
    for body in (english, japanese):
        assert "--repo <owner/repo> --branch <branch>" in body
    assert "no issue has to be created first" in english
    assert "issue を先に作る必要はありません" in japanese
