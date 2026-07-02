from guildbotics.capabilities.member_reference import capability_reference_text


def test_reference_covers_every_member_domain():
    text = capability_reference_text()
    # All capability domains are listed so any member can act across them
    # regardless of which workflow invoked it.
    assert "guildbotics member git commit" in text
    assert "guildbotics member github pr create" in text
    assert "guildbotics member chat reply" in text
    assert "guildbotics member task complete" in text
    assert "guildbotics member help" in text


def test_reference_includes_standard_work_procedure():
    text = capability_reference_text()
    # The mode-independent working procedure lives here so entrypoint prompts
    # (SKILL.md, workflow prompts) only carry their mode-specific envelope.
    assert "### Standard work procedure" in text
    assert "Inspect first" in text
    assert "before publishing any code change" in text
    assert "Stage with plain git" in text
    assert "reply_target_id" in text
    assert "Leave observable evidence" in text


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
