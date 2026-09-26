import pytest

from guildbotics.integrations.chat_workflow_status import (
    workflow_status_fields,
    workflow_status_metadata,
)
from guildbotics.integrations.slack.auth_errors import slack_api_error
from guildbotics.integrations.slack.message_events import chat_event


def test_chat_event_reads_a_thread_reply() -> None:
    metadata = workflow_status_metadata(
        workflow_status_fields(
            reason="failed", person_id="p1", source_event_id="C1:1.0", run_id="r1"
        )
    )

    event = chat_event(
        "C1",
        {
            "ts": "2.0",
            "thread_ts": "1.0",
            "user": "U1",
            "text": "hi <@U2> and <@U3>",
            "metadata": metadata,
        },
    )

    assert event is not None
    assert event.event_id == "C1:2.0"
    assert event.channel_id == "C1"
    assert event.message_ts == "2.0"
    assert event.thread_ts == "1.0"
    assert event.author_id == "U1"
    assert event.mentions == ["U2", "U3"]
    assert event.is_thread_reply is True
    assert event.is_bot_message is False
    assert event.metadata == metadata


def test_chat_event_starts_its_own_thread_without_thread_ts() -> None:
    event = chat_event("C1", {"ts": "1.0", "bot_id": "B1", "text": ""})

    assert event is not None
    assert event.thread_ts == "1.0"
    assert event.is_thread_reply is False
    assert event.is_bot_message is True
    assert event.author_id is None


@pytest.mark.parametrize(
    "raw",
    [
        {"text": "no timestamp"},
        {"ts": "", "text": "empty timestamp"},
        {"ts": "1.0", "subtype": "channel_join", "text": "joined"},
    ],
)
def test_chat_event_is_none_for_what_is_no_conversation(raw) -> None:
    assert chat_event("C1", raw) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": True, "url": "wss://example"}, ""),
        ({"ok": False, "error": "invalid_auth"}, "invalid_auth"),
        ({"ok": False}, "unknown_error"),
        ({}, "unknown_error"),
        (["not", "an", "object"], "invalid_json"),
    ],
)
def test_slack_api_error_names_the_code_of_a_failed_call(payload, expected) -> None:
    assert slack_api_error(payload) == expected
