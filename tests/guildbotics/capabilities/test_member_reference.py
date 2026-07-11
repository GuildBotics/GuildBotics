import pytest

from guildbotics.capabilities.member_reference import (
    capability_reference_text,
    command_summary,
)


def test_command_summary_comes_from_the_catalog():
    # The Click commands fill their missing --help text from this API, so the
    # catalog stays the single source of the one-line command purposes.
    assert (
        command_summary("git push")
        == "Push the current branch with the member credential."
    )


def test_command_summary_rejects_unknown_command_path():
    with pytest.raises(KeyError, match="no entry in the member capability catalog"):
        command_summary("git shove")


def test_reference_covers_every_member_domain():
    text = capability_reference_text()
    # All capability domains are listed so any member can act across them
    # regardless of which workflow invoked it.
    assert "guildbotics member git commit" in text
    assert "--include-diff" in text
    assert "guildbotics member github pr create" in text
    assert "guildbotics member github pr review-comment" in text
    assert "guildbotics member chat reply" in text
    assert "guildbotics member task complete" in text
    assert "guildbotics member help" in text


def test_reference_prepare_allows_issue_and_pr_url_together():
    text = capability_reference_text()
    # The CLI accepts --issue-url and --pr-url together (the ticket workflow's
    # PR-review path generates exactly that to check out the PR head). The
    # reference must not present them as mutually exclusive, or an agent building
    # the command itself could drop one and fall back to issue-branch mode.
    assert "--issue-url <url> [--pr-url <url>]" in text


def test_reference_includes_standard_work_procedure():
    text = capability_reference_text()
    # The mode-independent working procedure lives here so entrypoint prompts
    # (SKILL.md, workflow prompts) only carry their mode-specific envelope.
    assert "### Standard work procedure" in text
    assert "Inspect first" in text
    assert "before publishing any code change" in text
    assert "Stage with plain git" in text
    assert "files[].commentable_lines" in text
    assert "path`, `line`, `side`" in text
    assert "--start-line" in text
    assert "--start-side" in text
    assert "reply_target_id" in text
    # Observable-outcome principle: leave externally visible traces at the
    # work's entry point, and avoid duplicate / unwanted posts.
    assert "externally visible trace" in text
    assert "entry point" in text
    assert "Avoid duplicate posts" in text


def test_reference_states_pr_work_record_contract():
    text = capability_reference_text()
    # The PR work-record memory contract is shared by every entrypoint and must
    # not be re-stated in individual prompts.
    assert "member memory record --pr <pr_url>" in text
    assert "--ticket <url>" in text
    assert "--thread <url>" in text
    assert "remaining follow-up" in text
    assert "separate memory documents" in text


def test_reference_maps_communication_style_output_kinds():
    text = capability_reference_text()
    for output_kind in (
        "interactive_replies",
        "github_comments",
        "neutral_documents",
        "machine_outputs",
    ):
        assert output_kind in text


def test_reference_states_cross_cutting_rules():
    text = capability_reference_text()
    assert "### Rules" in text
    # Staging-is-plain-git and the writes-go-through-member guardrails are part of
    # the shared reference.
    assert "git add" in text
    assert "git config" in text
    assert "memory.pinned" in text
    assert "ticket URL, PR URL, Slack thread URL" in text
    assert "canonical current state" in text
    assert "autonomous workflow runs must propose policy" in text
    assert "use memory as the primary basis for the answer" in text
    assert "Never display, infer, store, or copy secrets" in text


def test_reference_excludes_task_contract():
    # Task contracts (primary objective / required completion semantics) belong to
    # each entrypoint, never to the shared reference, so workflow vs interactive
    # behavior is not blurred.
    text = capability_reference_text().lower()
    assert "primary objective" not in text
    assert "you must finish" not in text


def test_reference_excludes_ticket_specific_issue_comment_rule():
    """The shared reference states a generic observable-outcome principle, but
    must NOT contain the ticket-workflow-specific forced rule like 'Issue 起点
    なら必ずコメント' or 'always comment on the issue when a PR was created'.
    The command catalog naturally lists ``issue comment`` as an available
    capability, so we check only the procedure and rules sections."""
    text = capability_reference_text()
    # Extract Standard work procedure + Rules sections only
    procedure_and_rules = text.split("### Standard work procedure")[1]
    procedure_and_rules_lower = procedure_and_rules.lower()
    # Ticket-specific sentinel phrases must not appear in the procedure/rules.
    assert "member github issue comment" not in procedure_and_rules_lower
    assert "result comment" not in procedure_and_rules_lower
    assert "not a substitute" not in procedure_and_rules_lower
