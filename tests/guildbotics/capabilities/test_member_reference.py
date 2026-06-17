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


def test_reference_states_cross_cutting_rules():
    text = capability_reference_text()
    assert "### Rules" in text
    # Staging-is-plain-git and the writes-go-through-member guardrails are part of
    # the shared reference.
    assert "git add" in text
    assert "git config" in text
    assert "Never display, infer, store, or copy secrets" in text


def test_reference_excludes_task_contract():
    # Task contracts (primary objective / required completion semantics) belong to
    # each entrypoint, never to the shared reference, so workflow vs interactive
    # behavior is not blurred.
    text = capability_reference_text().lower()
    assert "primary objective" not in text
    assert "you must finish" not in text
