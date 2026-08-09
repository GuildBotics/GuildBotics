"""Layer-boundary tests for the member prompt sources.

Prompt layer model (see AGENTS.md「member プロンプト層モデル」):
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

from guildbotics.capabilities.member_reference import capability_reference_text
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
        # The guideline-check contract (origin default branch is canonical,
        # read via git fetch/show) lives in the member reference only.
        "git fetch origin",
        "git show origin",
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


def test_ticket_prompt_requires_issue_comment_on_pr_work():
    """handle_github_ticket must tell the agent to comment on the Issue when
    a PR is created/reused/updated, with PR URL and verification result."""
    for language in ("en", "ja"):
        body = _prompt_body("handle_github_ticket", language)
        assert "member github issue comment" in body, language
        assert "PR URL" in body or "PR url" in body, language
        # Verification result should be mentioned
        assert "verification" in body.lower() or "確認結果" in body, language


def test_ticket_prompt_clarifies_summary_is_not_github_substitute():
    """task complete --content-file and AgentResponse.message must be called
    out as NOT substitutes for GitHub comments, in both en and ja."""
    for language in ("en", "ja"):
        body = _prompt_body("handle_github_ticket", language)
        assert "task complete --content-file" in body, language
        assert "AgentResponse.message" in body, language
        # Both must appear in a context that says they are NOT substitutes
        assert "not a substitute" in body or "代替ではありません" in body, language


def test_issue_comment_contract_not_in_chat_prompt():
    """The Issue comment contract is specific to handle_github_ticket and
    must NOT leak into handle_chat_event."""
    for language in ("en", "ja"):
        body = _prompt_body("handle_chat_event", language)
        # The chat prompt should not contain the ticket-specific issue
        # comment instruction sentinel phrases.
        assert "task complete --content-file" not in body, language
        assert "not a substitute" not in body and "代替ではありません" not in body, (
            language
        )


def test_member_prompt_layers_use_shell_neutral_content_files():
    bodies = [
        capability_reference_text(),
        load_markdown_with_frontmatter(SKILL_PATH)["body"],
        *[
            _prompt_body(name, language)
            for name in WORKFLOW_PROMPTS
            for language in ("en", "ja")
        ],
    ]
    for body in bodies:
        assert "--content-stdin" not in body
        assert "<<'EOF'" not in body
        assert "--content-file" in body

    reference = bodies[0]
    assert "UTF-8" in reference
    assert "relative name with no spaces" in reference
    assert "delete it immediately" in reference


def test_interactive_skill_preserves_posix_path_and_documents_windows_bare_cli():
    body = load_markdown_with_frontmatter(SKILL_PATH)["body"]
    assert '"$HOME/.guildbotics/bin/guildbotics" member context' in body
    assert "On Windows" in body
    assert "guildbotics member context --person <person_id>" in body


def test_interactive_skill_stages_before_creating_commit_content_file():
    body = load_markdown_with_frontmatter(SKILL_PATH)["body"]
    assert (
        "stage the intended repository changes before creating the content file" in body
    )
    assert "Never stage the content file." in body


def _frontmatter(name: str, language: str) -> dict:
    return load_markdown_with_frontmatter(FUNCTIONS_DIR / f"{name}.{language}.md")


def test_ticket_work_runs_at_high_effort_by_default():
    """Ticket work is heavy by default; that decision lives in the frontmatter."""
    for language in ("en", "ja"):
        assert _frontmatter("handle_github_ticket", language)["effort"] == "high"


def test_chat_work_states_no_frontmatter_effort():
    """Chat effort is decided per event by the assessor, not fixed up front."""
    for language in ("en", "ja"):
        assert "effort" not in _frontmatter("handle_chat_event", language)


def test_the_effort_assessor_is_cheap_and_binary_across_languages():
    for language in ("en", "ja"):
        frontmatter = _frontmatter("assess_effort", language)
        # The assessment itself must not be the expensive call it is deciding.
        assert frontmatter["effort"] == "low"
        assert frontmatter["brain"] == "default"
        assert (
            frontmatter["response_class"]
            == "guildbotics.intelligences.common.EffortAssessmentResponse"
        )


def test_the_effort_assessor_prompts_share_placeholders_across_languages():
    english = set(_PLACEHOLDER_RE.findall(_prompt_body("assess_effort", "en")))
    japanese = set(_PLACEHOLDER_RE.findall(_prompt_body("assess_effort", "ja")))
    assert english == japanese
    assert english == {"latest_message", "previous_thread_context"}


def test_the_effort_assessor_never_offers_low_as_an_automatic_answer():
    """`low` has no defined automatic criterion, so it stays manual-only."""
    for language in ("en", "ja"):
        body = _prompt_body("assess_effort", language)
        assert "`high`" in body
        assert "`default`" in body
        assert "`low`" not in body


def test_the_effort_assessor_grades_repository_judgments_as_high():
    """Issue drafting and design-policy decisions for a repository are `high`
    (issue #381): they must be grounded in the repository's guidelines, which
    is workspace work even without code changes."""
    english = _prompt_body("assess_effort", "en")
    japanese = _prompt_body("assess_effort", "ja")
    assert "issue to be drafted or created" in english
    assert "policy decision" in english
    assert "guidelines" in english
    assert "issue の起票・作成" in japanese
    assert "方針の判断" in japanese
    assert "ガイドライン" in japanese


def test_chat_prompt_extends_checkout_to_repository_guideline_checks():
    """The chat workspace has no checkout, so the prompt must route guideline
    checks (issue #381) through `git prepare` — while the contract itself
    (origin default branch is canonical) stays in the member reference."""
    english = _prompt_body("handle_chat_event", "en")
    japanese = _prompt_body("handle_chat_event", "ja")
    assert "repository guideline check" in english
    assert "standard work procedure" in english
    assert "repository ガイドライン確認" in japanese
    assert "標準作業手順" in japanese
