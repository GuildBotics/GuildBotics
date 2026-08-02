from __future__ import annotations

import logging

import pytest

from guildbotics.intelligences.effort import (
    describe_overlay_problems,
    validate_effort_fields,
    EFFORT_LEVELS,
    EffortError,
    ResolvedEffort,
    effort_diagnostics,
    effort_settings,
    normalize_effort,
    promote_effort,
    resolve_effort,
    validate_effort_overlay,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("low", "low"),
        ("HIGH", "high"),
        ("  default  ", "default"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_effort_accepts_known_levels_and_blanks(value, expected) -> None:
    assert normalize_effort(value) == expected


def test_normalize_effort_rejects_an_explicit_unknown_level() -> None:
    with pytest.raises(EffortError) as excinfo:
        normalize_effort("extreme")
    assert "extreme" in str(excinfo.value)


def test_normalize_effort_downgrades_a_stored_unknown_level_with_a_warning(
    caplog,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert (
            normalize_effort("extreme", strict=False, logger=logging.getLogger()) == ""
        )
    assert "extreme" in caplog.text


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        # Ordering is low < default < high; a string max() would answer "low"
        # for the first case because "low" sorts above "high".
        ("high", "low", "high"),
        ("low", "high", "high"),
        ("default", "high", "high"),
        ("high", "default", "high"),
        ("", "default", "default"),
        ("high", "", "high"),
        ("", "", ""),
        ("high", "bogus", "high"),
    ],
)
def test_promote_effort_never_demotes(current, candidate, expected) -> None:
    assert promote_effort(current, candidate) == expected


def test_effort_levels_are_ordered_from_least_to_most() -> None:
    assert EFFORT_LEVELS == ("low", "default", "high")


def test_runtime_effort_wins_over_frontmatter() -> None:
    resolved = resolve_effort({"effort": "low"}, "high")
    assert resolved == ResolvedEffort(requested="low", resolved="low", source="runtime")


def test_runtime_default_cancels_a_frontmatter_high() -> None:
    """`default` is a decision, not an absence: it overrides frontmatter."""
    resolved = resolve_effort({"effort": "default"}, "high")
    assert resolved.resolved == "default"
    assert resolved.intervenes is False


def test_frontmatter_applies_when_no_runtime_effort_is_present() -> None:
    resolved = resolve_effort({"effort": ""}, "high")
    assert resolved == ResolvedEffort(
        requested="high", resolved="high", source="frontmatter"
    )


def test_nothing_specified_resolves_to_no_intervention() -> None:
    resolved = resolve_effort(None, "")
    assert resolved == ResolvedEffort()
    assert resolved.intervenes is False


def test_validate_effort_overlay_accepts_known_levels() -> None:
    assert validate_effort_overlay(
        {"high": {"reasoning_effort": "high"}, "low": {"thinking_budget": 0}},
        where="model 'x'",
    ) == {"high": {"reasoning_effort": "high"}, "low": {"thinking_budget": 0}}


def test_validate_effort_overlay_accepts_an_absent_block() -> None:
    assert validate_effort_overlay(None, where="model 'x'") == {}


@pytest.mark.parametrize(
    "overlay",
    [
        {"extreme": {"a": 1}},
        {"high": "reasoning_effort=high"},
        ["high"],
    ],
)
def test_validate_effort_overlay_rejects_malformed_blocks(overlay) -> None:
    with pytest.raises(EffortError):
        validate_effort_overlay(overlay, where="model 'x'")


def test_effort_settings_returns_the_mapping_for_the_resolved_level() -> None:
    overlay = {"high": {"reasoning_effort": "high"}}
    resolved = ResolvedEffort(requested="high", resolved="high", source="runtime")
    assert effort_settings(overlay, resolved) == {"reasoning_effort": "high"}


def test_effort_settings_does_not_intervene_for_default_or_unspecified() -> None:
    overlay = {"high": {"reasoning_effort": "high"}}
    assert effort_settings(overlay, ResolvedEffort(resolved="default")) == {}
    assert effort_settings(overlay, ResolvedEffort()) == {}


def test_missing_mapping_warns_and_continues_instead_of_failing(caplog) -> None:
    resolved = ResolvedEffort(requested="high", resolved="high", source="runtime")
    with caplog.at_level(logging.WARNING):
        settings = effort_settings({}, resolved, logger=logging.getLogger())
    assert settings == {}
    assert "high" in caplog.text


def test_diagnostics_record_key_names_only_never_setting_values() -> None:
    resolved = ResolvedEffort(requested="HIGH", resolved="high", source="runtime")
    payload = effort_diagnostics(
        resolved,
        {"reasoning_effort": "high", "api_key": "sk-secret"},
        model="gpt-x",
    )
    assert payload == {
        "requested": "HIGH",
        "resolved": "high",
        "source": "runtime",
        "applied_keys": ["api_key", "reasoning_effort"],
        "unsupported": False,
        "model": "gpt-x",
    }
    assert "sk-secret" not in str(payload)


def test_diagnostics_flag_an_explicit_level_with_no_mapping_as_unsupported() -> None:
    resolved = ResolvedEffort(requested="high", resolved="high", source="frontmatter")
    payload = effort_diagnostics(resolved, {})
    assert payload["unsupported"] is True
    assert payload["applied_keys"] == []


def test_field_descriptors_reject_a_malformed_block() -> None:
    """A broken descriptor is an error, not something to skip silently.

    Dropping it would degrade the editor to free-text with no indication why.
    """
    with pytest.raises(EffortError):
        validate_effort_fields([{"key": "x"}], where="test")
    with pytest.raises(EffortError):
        validate_effort_fields([{"key": "x", "type": "enum"}], where="test")
    with pytest.raises(EffortError):
        validate_effort_fields([{"key": "x", "type": "nope"}], where="test")
    assert validate_effort_fields(None, where="test") == []


def test_a_mistyped_key_is_reported_against_the_descriptors() -> None:
    fields = validate_effort_fields(
        [
            {"key": "reasoning_effort", "type": "enum", "values": ["low", "high"]},
            {"key": "id", "type": "model_id"},
        ],
        where="test",
    )
    problems = describe_overlay_problems({"high": {"reasoning_efort": "high"}}, fields)
    assert problems and "reasoning_efort" in problems[0]
    assert (
        describe_overlay_problems({"high": {"reasoning_effort": "high"}}, fields) == []
    )


def test_nested_and_numeric_settings_are_checked_by_dotted_path() -> None:
    fields = validate_effort_fields(
        [
            {"key": "thinking.type", "type": "enum", "values": ["enabled", "disabled"]},
            {"key": "thinking.budget_tokens", "type": "integer", "minimum": 1024},
        ],
        where="test",
    )
    valid = {"high": {"thinking": {"type": "enabled", "budget_tokens": 8000}}}
    assert describe_overlay_problems(valid, fields) == []

    below = {"high": {"thinking": {"type": "enabled", "budget_tokens": 10}}}
    assert "at least 1024" in describe_overlay_problems(below, fields)[0]

    stringified = {"high": {"thinking": {"budget_tokens": "8000"}}}
    assert "whole number" in describe_overlay_problems(stringified, fields)[0]

    bad_enum = {"high": {"thinking": {"type": "on"}}}
    assert "must be one of" in describe_overlay_problems(bad_enum, fields)[0]


def test_a_provider_without_descriptors_accepts_anything() -> None:
    """Nothing is known about its keys, so nothing can be called wrong."""
    assert describe_overlay_problems({"high": {"whatever": 1}}, []) == []


def test_a_mapping_for_default_is_rejected_rather_than_ignored() -> None:
    """`default` means "do not intervene", so such a mapping can never fire.

    Accepting it would store configuration that looks live, applies nothing, and
    gives no hint why.
    """
    with pytest.raises(EffortError) as error:
        validate_effort_overlay({"default": {"reasoning_effort": "medium"}}, where="m")

    assert "parameters" in str(error.value)
    assert validate_effort_overlay({"low": {}, "high": {}}, where="m") == {
        "low": {},
        "high": {},
    }


def test_default_remains_a_valid_request_even_though_it_maps_to_nothing() -> None:
    """The restriction is on mappings, not on what a command may ask for."""
    assert normalize_effort("default") == "default"
    assert resolve_effort({"effort": "default"}, "high").resolved == "default"
