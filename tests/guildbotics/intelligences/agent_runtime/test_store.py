from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from guildbotics.intelligences.agent_runtime.models import (
    ConversationKey,
    ResumePolicy,
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


def test_model_change_and_explicit_reset_rotate_atomically(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    record = store.resolve(_key(), ResumePolicy.AUTO, model="old")
    record.provider_session_id = "thread-1"
    store.save(record)

    changed = store.resolve(_key(), ResumePolicy.AUTO, model="new")
    assert changed.generation == 1
    assert changed.rotation_reason == "model_changed"
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
