from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
    ResumePolicy,
    settings_fingerprint,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore


def _key(identity: str = "issue-300") -> ConversationKey:
    return ConversationKey("aiko", "codex", "ticket", identity)


def test_conversation_store_resumes_exact_session_and_separates_keys(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, model="gpt-5")
    record.provider_session_id = "thread-1"
    record.context_cursor = "cursor-1"
    store.save(record)

    resumed = store.resolve(_key(), ResumePolicy.RESUME, model="gpt-5")

    assert resumed.provider_session_id == "thread-1"
    assert resumed.context_cursor == "cursor-1"
    assert store.resolve(_key("issue-301"), ResumePolicy.AUTO).provider_session_id == ""


def test_resume_rejects_missing_or_unhealthy_exact_session(tmp_path) -> None:
    store = ConversationStore(tmp_path)

    with pytest.raises(LookupError, match="exact conversation"):
        store.resolve(_key(), ResumePolicy.RESUME)

    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    store.mark_unhealthy(record, "process")

    with pytest.raises(LookupError, match="healthy provider session"):
        store.resolve(_key(), ResumePolicy.RESUME)


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (lambda record: setattr(record, "healthy", False), "unhealthy_session"),
        (lambda record: setattr(record, "turn_count", 2), "turn_limit"),
        (lambda record: setattr(record, "input_tokens", 10), "usage_limit"),
        (
            lambda record: setattr(
                record,
                "updated_at",
                (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            ),
            "ttl_expired",
        ),
    ],
)
def test_auto_rotation_clears_provider_state(
    tmp_path, mutation, expected_reason
) -> None:
    store = ConversationStore(
        tmp_path,
        ttl=timedelta(hours=1),
        max_turns=2,
        max_tokens=10,
    )
    record = store.resolve(_key(), ResumePolicy.AUTO, model="old")
    record.provider_session_id = "thread-1"
    record.provider_turn_id = "turn-1"
    record.context_cursor = "cursor-1"
    mutation(record)
    store.save(record)
    if not record.healthy:
        record.rotation_reason = ""
        store.save(record)
    if expected_reason == "ttl_expired":
        store = ConversationStore(
            tmp_path,
            ttl=timedelta(microseconds=-1),
            max_turns=2,
            max_tokens=10,
        )

    rotated = store.resolve(_key(), ResumePolicy.AUTO, model="old")

    assert rotated.generation == 1
    assert rotated.rotation_reason == expected_reason
    assert rotated.provider_session_id == ""
    assert rotated.provider_turn_id == ""
    assert rotated.context_cursor == ""


def test_settings_change_and_explicit_reset_rotate_atomically(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(
        _key(), ResumePolicy.AUTO, model="old", settings_fingerprint="fp-old"
    )
    record.provider_session_id = "thread-1"
    store.save(record)

    changed = store.resolve(
        _key(), ResumePolicy.AUTO, model="new", settings_fingerprint="fp-new"
    )
    assert changed.generation == 1
    assert changed.rotation_reason == "settings_changed"
    store.save(changed)

    reset = store.resolve(_key(), ResumePolicy.RESET, model="new")
    store.save(reset)
    loaded = store.load(_key())

    assert loaded is not None
    assert loaded.generation == 2
    assert loaded.rotation_reason == "reset"
    payload = json.loads(
        next((tmp_path / "agent-runtime/conversations").rglob("*.json")).read_text()
    )
    assert payload["version"] == 1
    serialized = json.dumps(payload).lower()
    assert "access_token" not in serialized
    assert "authorization" not in serialized


def test_fresh_policy_never_reuses_a_persisted_provider_session(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    record.context_cursor = "cursor-1"
    store.save(record)

    fresh = store.resolve(_key(), ResumePolicy.FRESH)

    assert fresh.generation == 1
    assert fresh.rotation_reason == "fresh"
    assert fresh.provider_session_id == ""
    assert fresh.context_cursor == ""


def test_round_trips_last_run_and_event_identity(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    record.last_run_id = "run-A"
    record.last_event_id = "EA"
    store.save(record)

    loaded = store.load(_key())

    assert loaded is not None
    assert loaded.last_run_id == "run-A"
    assert loaded.last_event_id == "EA"


def test_rotation_clears_last_run_and_event_identity(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    record.last_run_id = "run-A"
    record.last_event_id = "EA"
    store.save(record)

    rotated = store.resolve(_key(), ResumePolicy.RESET)

    assert rotated.last_run_id == ""
    assert rotated.last_event_id == ""


def test_context_snapshot_round_trips_and_defaults_to_zero(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "sess-1"
    record.context_used_tokens = 120_000
    record.context_size_tokens = 500_000
    store.save(record)

    reloaded = store.load(_key())

    assert reloaded is not None
    assert reloaded.context_used_tokens == 120_000
    assert reloaded.context_size_tokens == 500_000
    assert (
        ConversationStore(tmp_path / "other")
        .resolve(_key(), ResumePolicy.AUTO)
        .context_size_tokens
        == 0
    )


def test_context_utilization_at_or_above_the_limit_rotates(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "sess-1"
    record.context_used_tokens = 450_000
    record.context_size_tokens = 500_000
    store.save(record)

    rotated = store.resolve(_key(), ResumePolicy.AUTO)

    assert rotated.rotation_reason == "context_limit"
    assert rotated.provider_session_id == ""
    assert rotated.context_used_tokens == 0
    assert rotated.context_size_tokens == 0
    assert rotated.generation == 1


def test_context_utilization_below_the_limit_keeps_the_session(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "sess-1"
    record.context_used_tokens = 449_999
    record.context_size_tokens = 500_000
    store.save(record)

    kept = store.resolve(_key(), ResumePolicy.AUTO)

    assert kept.provider_session_id == "sess-1"
    assert kept.rotation_reason == ""


def test_unusable_context_size_is_never_used_for_rotation(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "sess-1"
    record.context_used_tokens = 900_000
    record.context_size_tokens = 0
    store.save(record)

    kept = store.resolve(_key(), ResumePolicy.AUTO)

    assert kept.provider_session_id == "sess-1"
    assert kept.rotation_reason == ""


def test_stored_record_without_context_fields_loads_as_zero(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "sess-1"
    store.save(record)
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    del payload["context_used_tokens"]
    del payload["context_size_tokens"]
    path.write_text(json.dumps(payload))

    reloaded = store.load(_key())

    assert reloaded is not None
    assert reloaded.context_used_tokens == 0
    assert reloaded.context_size_tokens == 0


# --------------------------------------------------------------------------- #
# Effort: settings fingerprints and the "keep the session as it is" rule
# --------------------------------------------------------------------------- #


def _context(**overrides) -> AgentExecutionContext:
    defaults = {
        "person_id": "aiko",
        "run_id": "run-1",
        "cwd": Path("/tmp"),
        "workspace_data_root": Path("/tmp"),
        "conversation_key": _key(),
    }
    return AgentExecutionContext(**{**defaults, **overrides})


def test_a_turn_that_imposes_nothing_has_an_empty_fingerprint() -> None:
    assert settings_fingerprint({}) == ""


def test_the_fingerprint_is_stable_and_distinguishes_applied_settings() -> None:
    base = settings_fingerprint({"model": "m", "max_thinking_tokens": 8000})
    assert base != ""
    assert base == settings_fingerprint({"max_thinking_tokens": 8000, "model": "m"})
    assert base != settings_fingerprint({"model": "m", "max_thinking_tokens": 1000})
    # Model alone still distinguishes two sessions, as it did before effort.
    assert settings_fingerprint({"model": "old"}) != settings_fingerprint(
        {"model": "new"}
    )


def test_a_request_the_adapter_cannot_apply_is_not_a_settings_change() -> None:
    """The fingerprint follows what is applied, not what was asked for.

    Grok ignores every effort setting, and an unmapped level applies nothing at
    all. Either way the provider configuration is untouched, so the session must
    not be rotated and its history thrown away.
    """
    assert settings_fingerprint({}) == settings_fingerprint({})


def test_two_different_stated_settings_rotate_the_session(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-a")
    record.provider_session_id = "thread-1"
    store.save(record)

    changed = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-b")

    assert changed.rotation_reason == "settings_changed"
    assert changed.provider_session_id == ""
    assert changed.settings_fingerprint == "fp-b"


def test_dropping_to_no_stated_settings_keeps_the_session(tmp_path) -> None:
    """`default` / unspecified means "keep the current settings", not "reset"."""
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-high")
    record.provider_session_id = "thread-1"
    store.save(record)

    kept = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="")

    assert kept.rotation_reason == ""
    assert kept.provider_session_id == "thread-1"
    assert kept.settings_fingerprint == "fp-high"


def test_stating_settings_for_the_first_time_keeps_the_session(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    store.save(record)

    adopted = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-high")

    assert adopted.rotation_reason == ""
    assert adopted.provider_session_id == "thread-1"
    assert adopted.settings_fingerprint == "fp-high"


def test_a_rotated_session_forgets_the_settings_it_was_running(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-high")
    record.provider_session_id = "thread-1"
    store.save(record)

    fresh = store.resolve(_key(), ResumePolicy.FRESH)

    assert fresh.settings_fingerprint == ""


def test_the_fingerprint_survives_a_save_reload_round_trip(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, settings_fingerprint="fp-high")
    store.save(record)

    loaded = store.load(_key())

    assert loaded is not None
    assert loaded.settings_fingerprint == "fp-high"
