from __future__ import annotations

from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_workflow_status import (
    is_suppressed_chat_event,
    is_suppressed_workflow_status_metadata,
    is_workflow_status_metadata,
    workflow_status_metadata,
)


def test_workflow_status_metadata_builds_suppressed_payload():
    metadata = workflow_status_metadata(
        reason="rate_limited",
        person_id="alice",
        source_event_id="C1:100.1",
        run_id="run-1",
        retry_after_at="2026-07-04T11:44:00+09:00",
        retry_after_text="11:44 AM",
    )

    assert is_workflow_status_metadata(metadata) is True
    assert is_suppressed_workflow_status_metadata(metadata) is True
    assert metadata["event_type"] == "guildbotics.workflow_status"
    assert metadata["event_payload"]["reason"] == "rate_limited"
    assert metadata["event_payload"]["routing"] == "suppress"


def test_other_or_malformed_metadata_is_not_suppressed():
    assert (
        is_workflow_status_metadata({"event_type": "other", "event_payload": {}})
        is False
    )
    assert (
        is_suppressed_workflow_status_metadata(
            {"event_type": "guildbotics.workflow_status", "event_payload": "bad"}
        )
        is False
    )
    assert (
        is_suppressed_workflow_status_metadata(
            {
                "event_type": "guildbotics.workflow_status",
                "event_payload": {"routing": "normal"},
            }
        )
        is False
    )


def test_suppressed_chat_event_uses_event_metadata():
    event = ChatEvent(
        event_id="C1:100.1",
        channel_id="C1",
        message_ts="100.1",
        thread_ts="100.1",
        author_id="U1",
        text="notice",
        metadata=workflow_status_metadata(
            reason="failed",
            person_id="alice",
            source_event_id="C1:99.9",
            run_id="run-1",
        ),
    )

    assert is_suppressed_chat_event(event) is True
