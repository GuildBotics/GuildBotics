"""The workflow status a Slack message carries, as its message metadata."""

from __future__ import annotations

import pytest

from guildbotics.integrations.slack.workflow_status import (
    metadata_status,
    status_metadata,
)
from guildbotics.runtime.port import WorkflowStatus


def test_a_status_comes_back_from_the_metadata_that_carries_it() -> None:
    status = WorkflowStatus(
        reason="rate_limited",
        person_id="alice",
        run_id="run-1",
        subject="C1:100.1",
        retry_after_at="2026-07-04T11:44:00+09:00",
        retry_after_text="11:44 AM",
    )

    assert metadata_status(status_metadata(status)) == status


def test_the_metadata_names_itself_and_asks_to_be_suppressed() -> None:
    status = WorkflowStatus(
        reason="failed", person_id="alice", run_id="run-1", subject="C1:99.9"
    )

    assert status_metadata(status) == {
        "event_type": "guildbotics.workflow_status",
        "event_payload": {
            "kind": "workflow_error",
            "routing": "suppress",
            "reason": "failed",
            "person_id": "alice",
            "run_id": "run-1",
            "subject": "C1:99.9",
        },
    }


def test_empty_fields_are_left_out_of_the_metadata() -> None:
    status = WorkflowStatus(reason="failed", person_id="alice", run_id="")

    payload = status_metadata(status)["event_payload"]

    assert payload == {
        "kind": "workflow_error",
        "routing": "suppress",
        "reason": "failed",
        "person_id": "alice",
    }
    assert metadata_status(status_metadata(status)) == status


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "not a mapping",
        {"event_type": "other", "event_payload": {"kind": "workflow_error"}},
        {"event_type": "guildbotics.workflow_status", "event_payload": "bad"},
        {
            "event_type": "guildbotics.workflow_status",
            "event_payload": {"kind": "other", "reason": "failed", "person_id": "a"},
        },
        {
            "event_type": "guildbotics.workflow_status",
            "event_payload": {"kind": "workflow_error", "reason": "unknown"},
        },
        {
            "event_type": "guildbotics.workflow_status",
            "event_payload": {"kind": "workflow_error", "person_id": "alice"},
        },
    ],
)
def test_metadata_that_is_no_status_carries_none(metadata) -> None:
    assert metadata_status(metadata) is None


def test_fields_a_status_does_not_have_are_passed_over() -> None:
    metadata = {
        "event_type": "guildbotics.workflow_status",
        "event_payload": {
            "kind": "workflow_error",
            "routing": "suppress",
            "reason": "failed",
            "person_id": "alice",
            "source_event_id": "C1:99.9",
        },
    }

    assert metadata_status(metadata) == WorkflowStatus(
        reason="failed", person_id="alice", run_id=""
    )
