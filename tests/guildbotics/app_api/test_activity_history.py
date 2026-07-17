"""Session-title behaviour for the activity history normalizer.

Chat-triggered workflows carry the agent's system prompt as payload, so the
title must never fall through to that prompt (every chat session shares the
same prompt head). It should prefer the completion summary, then a
provider-neutral trigger label while the run is still in progress.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from guildbotics.app_api.activity_history import (
    _OWNER_TRACE_KEY,
    build_activity_history,
)
from guildbotics.entities.team import Person
from guildbotics.utils.i18n_tool import set_language, t

START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 2, tzinfo=UTC)
LONG_PROMPT = (
    "あなたは Slack thread の文脈を理解し、割り当てられた GuildBotics member として..."
)
CHAT_SUBJECT_ID = "slack:C1:100.1:E1"
TICKET_SUBJECT_ID = "https://github.com/o/r/issues/42"


def _ticket_records() -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "t-ticket",
            "person_id": "alice",
            "timestamp": "2026-07-01T11:00:00+00:00",
            "source": "scheduled",
            "command": "workflows/ticket_driven_workflow",
            "workflow": "",
            "attributes": {
                "github.url": TICKET_SUBJECT_ID,
                "github.number": "42",
                "github.kind": "issue",
            },
            "payload": {},
        }
    ]


def _members() -> list[Person]:
    return [
        Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
    ]


def _chat_records() -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "t-chat",
            "person_id": "alice",
            "timestamp": "2026-07-01T10:00:00+00:00",
            "source": "event_listener",
            "command": "workflows/chat_conversation_workflow",
            "workflow": "",
            "kind": "io",
            "attributes": {
                "service_run_id": "scheduler-run",
                "event.provider": "slack",
                "slack.channel": "C1",
                "slack.thread_ts": "100.1",
                "event_id": "E1",
            },
            "payload": {"fields": {"prompt": LONG_PROMPT}},
        }
    ]


def _members_pair() -> list[Person]:
    return [
        Person(person_id="alice", name="Alice", person_type="agent", is_active=True),
        Person(person_id="kenji", name="Kenji", person_type="agent", is_active=True),
    ]


def _session(
    records: list[dict[str, Any]],
    run_summary: Callable[[str, str], str],
    run_subject: Callable[[str], str] = lambda _run_id: "",
    members: list[Person] | None = None,
) -> Any:
    history = build_activity_history(
        start=START,
        end=END,
        members=members or _members(),
        records=records,
        run_summary=run_summary,
        run_subject=run_subject,
    )
    assert len(history.sessions) == 1
    return history.sessions[0]


def _title(
    records: list[dict[str, Any]], run_summary: Callable[[str, str], str]
) -> str:
    return str(_session(records, run_summary).title)


def test_completed_chat_session_titled_by_summary_first_line() -> None:
    title = _title(
        _chat_records(),
        lambda subject_id, person_id: (
            "請求プランの質問に回答\n詳細は省略"
            if subject_id == CHAT_SUBJECT_ID
            else ""
        ),
    )
    assert title == "請求プランの質問に回答"


def test_in_progress_chat_session_uses_neutral_trigger_label() -> None:
    set_language("ja")
    title = _title(_chat_records(), lambda _subject_id, _person_id: "")
    assert title == t("app_api.activity_history.chat_trigger", provider="Slack")
    assert LONG_PROMPT not in title


def test_rate_limited_workflow_event_sets_session_rate_limit() -> None:
    session = _session(
        [
            {
                "trace_id": "t-rate",
                "person_id": "alice",
                "timestamp": "2026-07-01T10:00:00+00:00",
                "source": "event_listener",
                "command": "workflows/chat_conversation_workflow",
                "workflow": "workflows/chat_conversation_workflow",
                "kind": "event",
                "type": "workflow.rate_limited",
                "attributes": {
                    "rate_limit.retry_after_at": "2026-07-04T11:44:00+09:00",
                    "rate_limit.retry_after_text": "11:44 AM",
                },
                "payload": {
                    "retry_after_at": "fallback",
                    "retry_after_text": "fallback",
                },
            }
        ],
        lambda _subject_id, _person_id: "",
    )

    assert session.status == "rate_limited"
    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_at == "2026-07-04T11:44:00+09:00"
    assert session.rate_limit.retry_after_text == "11:44 AM"


def test_rate_limited_ticket_workflow_event_sets_session_rate_limit() -> None:
    session = _session(
        [
            {
                "trace_id": "t-rate",
                "person_id": "alice",
                "timestamp": "2026-07-01T10:00:00+00:00",
                "source": "routine",
                "command": "workflows/ticket_driven_workflow",
                "workflow": "workflows/ticket_driven_workflow",
                "kind": "event",
                "type": "workflow.rate_limited",
                "attributes": {
                    "rate_limit.retry_after_at": "2026-07-05T12:00:00+09:00",
                    "rate_limit.retry_after_text": "12:00 PM",
                },
                "payload": {},
            }
        ],
        lambda _subject_id, _person_id: "",
    )

    assert session.status == "rate_limited"
    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_at == "2026-07-05T12:00:00+09:00"
    assert session.rate_limit.retry_after_text == "12:00 PM"


def test_latest_rate_limit_record_wins() -> None:
    session = _session(
        [
            {
                "trace_id": "t-rate",
                "person_id": "alice",
                "timestamp": "2026-07-01T10:00:00+00:00",
                "source": "event_listener",
                "command": "workflows/chat_conversation_workflow",
                "kind": "event",
                "type": "workflow.rate_limited",
                "attributes": {"rate_limit.retry_after_text": "old"},
                "payload": {},
            },
            {
                "trace_id": "t-rate",
                "person_id": "alice",
                "timestamp": "2026-07-01T10:05:00+00:00",
                "source": "event_listener",
                "command": "workflows/chat_conversation_workflow",
                "kind": "event",
                "type": "workflow.rate_limited",
                "attributes": {},
                "payload": {"retry_after_text": "new"},
            },
        ],
        lambda _subject_id, _person_id: "",
    )

    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_text == "new"


def test_completed_ticket_session_titled_by_summary_keeps_issue_link() -> None:
    records = [
        {
            "trace_id": "t-ticket",
            "person_id": "alice",
            "timestamp": "2026-07-01T11:00:00+00:00",
            "source": "scheduled",
            "command": "workflows/ticket_driven_workflow",
            "workflow": "",
            "attributes": {
                "service_run_id": "run-2",
                "github.url": "https://github.com/o/r/issues/42",
                "github.number": "42",
                "github.kind": "issue",
            },
            "payload": {},
        }
    ]
    session = _session(
        records, lambda _subject_id, _person_id: "Issue #42 のバグを修正"
    )
    assert session.title == "Issue #42 のバグを修正"
    assert any(
        link.kind == "issue" and link.label == "Issue #42" for link in session.links
    )


def test_ticket_session_without_summary_falls_back_to_issue_link() -> None:
    records = [
        {
            "trace_id": "t-ticket",
            "person_id": "alice",
            "timestamp": "2026-07-01T11:00:00+00:00",
            "source": "scheduled",
            "command": "workflows/ticket_driven_workflow",
            "workflow": "",
            "attributes": {
                "github.url": "https://github.com/o/r/issues/42",
                "github.number": "42",
                "github.kind": "issue",
            },
            "payload": {},
        }
    ]
    assert _title(records, lambda _subject_id, _person_id: "") == "Issue #42"


def test_workflow_memory_write_links_back_to_owning_session() -> None:
    memory_record = {
        "trace_id": None,
        "person_id": "alice",
        "timestamp": "2026-07-01T10:01:00+00:00",
        "kind": "memory",
        "type": "memory.update",
        "attributes": {
            "run_id": "task-run-1",
            "memory.action": "update",
            "memory.doc_id": "doc-xyz",
            "memory.path": "documents/personal/alice/doc-xyz",
        },
        "payload": {"title": "PR #244 の作業記録"},
    }
    session = _session(
        _chat_records() + [memory_record],
        run_summary=lambda _subject_id, _person_id: "",
        run_subject=lambda run_id: CHAT_SUBJECT_ID if run_id == "task-run-1" else "",
    )
    doc_links = [link for link in session.links if link.kind == "doc"]
    assert [link.label for link in doc_links] == ["PR #244 の作業記録"]
    # The memory link must keep the memory event's own identity (doc_id), not
    # the owning chat trace, or the diagnostics memory tab filters to nothing.
    assert "doc_id=doc-xyz" in doc_links[0].url
    assert "t-chat" not in doc_links[0].url


def test_ticket_workflow_memory_write_uses_task_run_id() -> None:
    # Ticket workflows tag memory records with ``task_run_id`` (not ``run_id``);
    # the record must still be adopted into the owning ticket session.
    memory_record = {
        "trace_id": None,
        "person_id": "alice",
        "timestamp": "2026-07-01T11:01:00+00:00",
        "kind": "memory",
        "type": "memory.update",
        "attributes": {
            "task_run_id": "ticket-run",
            "memory.action": "update",
            "memory.doc_id": "ticket-doc",
            "memory.path": "documents/personal/alice/ticket-doc",
        },
        "payload": {"title": "Issue #42 の作業記録"},
    }
    session = _session(
        _ticket_records() + [memory_record],
        run_summary=lambda _subject_id, _person_id: "",
        run_subject=lambda run_id: TICKET_SUBJECT_ID if run_id == "ticket-run" else "",
    )
    assert any(
        link.kind == "doc" and link.label == "Issue #42 の作業記録"
        for link in session.links
    )


def test_build_activity_history_does_not_mutate_input_records() -> None:
    memory_record = {
        "trace_id": None,
        "person_id": "alice",
        "timestamp": "2026-07-01T10:01:00+00:00",
        "kind": "memory",
        "type": "memory.update",
        "attributes": {"run_id": "task-run-1", "memory.doc_id": "doc-xyz"},
        "payload": {"title": "PR #244 の作業記録"},
    }
    records = _chat_records() + [memory_record]
    build_activity_history(
        start=START,
        end=END,
        members=_members(),
        records=records,
        run_summary=lambda _subject_id, _person_id: "",
        run_subject=lambda run_id: CHAT_SUBJECT_ID if run_id == "task-run-1" else "",
    )
    # The adopted record was copied, so the caller's dicts stay clean and reusing
    # the same array under different conditions cannot leak a stale owner trace.
    assert all(_OWNER_TRACE_KEY not in record for record in records)


def test_read_only_memory_record_is_not_adopted_into_session() -> None:
    # A `get`/`recall`/`touch` does not change a document, so it must not add a
    # link nor become the session title (its payload title is just what was read).
    read_record = {
        "trace_id": None,
        "person_id": "alice",
        "timestamp": "2026-07-01T10:01:00+00:00",
        "kind": "memory",
        "type": "memory.get",
        "attributes": {
            "run_id": "task-run-1",
            "memory.action": "get",
            "memory.doc_id": "read-doc",
            "memory.path": "documents/personal/alice/read-doc",
        },
        "payload": {"title": "読んだだけのメモ"},
    }
    set_language("ja")
    session = _session(
        _chat_records() + [read_record],
        run_summary=lambda _subject_id, _person_id: "",
        run_subject=lambda run_id: CHAT_SUBJECT_ID if run_id == "task-run-1" else "",
    )
    assert all(link.kind != "doc" for link in session.links)
    assert session.title == t("app_api.activity_history.chat_trigger", provider="Slack")


def test_read_only_memory_title_is_skipped_for_session_title() -> None:
    # An interactive session that recalled memory then wrote a note must title
    # from the note it wrote, not the generic "Memory recall" of the read event.
    base = {
        "trace_id": "t-int",
        "person_id": "alice",
        "source": "interactive",
        "command": "member chat reply",
        "workflow": "",
        "kind": "io",
        "attributes": {},
        "payload": {"fields": {"prompt": "作業して"}},
    }
    recall = {
        "trace_id": "t-int",
        "person_id": "alice",
        "timestamp": "2026-07-01T10:01:00+00:00",
        "kind": "memory",
        "type": "memory.recall",
        "attributes": {"memory.action": "recall"},
        "payload": {"title": "Memory recall"},
    }
    wrote = {
        "trace_id": "t-int",
        "person_id": "alice",
        "timestamp": "2026-07-01T10:02:00+00:00",
        "kind": "memory",
        "type": "memory.update",
        "attributes": {"memory.action": "update", "memory.doc_id": "d1"},
        "payload": {"title": "PR #247: レビュー対応"},
    }
    base_with_ts = {**base, "timestamp": "2026-07-01T10:00:00+00:00"}
    title = _title([base_with_ts, recall, wrote], lambda _subject_id, _person_id: "")
    assert title == "PR #247: レビュー対応"


def test_memory_write_does_not_cross_to_another_members_session() -> None:
    # Alice owns the chat session for the shared Slack subject; Kenji ran his
    # own workflow against the same subject and recalled memory. Kenji's memory
    # record must not be adopted into Alice's session.
    kenji_memory = {
        "trace_id": None,
        "person_id": "kenji",
        "timestamp": "2026-07-01T10:01:00+00:00",
        "kind": "memory",
        "type": "memory.recall",
        "attributes": {
            "run_id": "kenji-run",
            "memory.action": "recall",
            "memory.doc_id": "kenji-doc",
            "memory.path": "documents/personal/kenji/kenji-doc",
        },
        "payload": {"title": "Kenji recall"},
    }
    session = _session(
        _chat_records() + [kenji_memory],
        run_summary=lambda _subject_id, _person_id: "",
        run_subject=lambda run_id: CHAT_SUBJECT_ID if run_id == "kenji-run" else "",
        members=_members_pair(),
    )
    assert session.person_id == "alice"
    assert all(link.label != "Kenji recall" for link in session.links)


def test_interactive_session_still_uses_prompt() -> None:
    records = [
        {
            "trace_id": "t-interactive",
            "person_id": "alice",
            "timestamp": "2026-07-01T12:00:00+00:00",
            "source": "interactive",
            "command": "member chat reply",
            "workflow": "",
            "kind": "io",
            "attributes": {},
            "payload": {"fields": {"prompt": "会議の議事録をまとめて"}},
        }
    ]
    title = _title(records, lambda _subject_id, _person_id: "")
    assert title == "会議の議事録をまとめて"


def test_issue_create_event_and_comments_share_one_session_issue_link() -> None:
    issue_url = "https://github.com/o/r/issues/43"
    records = [
        {
            "trace_id": "t-issue",
            "person_id": "alice",
            "timestamp": "2026-07-01T12:00:00+00:00",
            "source": "interactive",
            "kind": "event",
            "type": "github.issue",
            "attributes": {
                "github.action": "opened",
                "github.kind": "issue",
                "github.number": 43,
                "github.repo": "o/r",
                "github.url": issue_url,
            },
            "payload": {
                "action": "opened",
                "issue": {
                    "number": 43,
                    "title": "Track issue activity",
                    "html_url": issue_url,
                },
            },
        },
        *[
            {
                "trace_id": "t-issue",
                "person_id": "alice",
                "timestamp": f"2026-07-01T12:0{index}:00+00:00",
                "source": "interactive",
                "kind": "event",
                "type": "github.issue_comment",
                "attributes": {
                    "github.action": "commented",
                    "github.kind": "issue",
                    "github.number": 43,
                    "github.repo": "o/r",
                    "github.url": issue_url,
                },
                "payload": {
                    "action": "commented",
                    "issue": {"number": 43, "html_url": issue_url},
                    "comment": {
                        "id": index,
                        "html_url": f"{issue_url}#issuecomment-{index}",
                    },
                },
            }
            for index in (1, 2)
        ],
    ]

    history = build_activity_history(
        start=START,
        end=END,
        members=_members(),
        records=records,
    )

    assert [(event.type, event.title, event.detail) for event in history.events] == [
        ("issue_create", "Issue #43 Created", "Track issue activity")
    ]
    assert history.events[0].url == issue_url
    assert [
        (link.kind, link.label, link.url) for link in history.sessions[0].links
    ] == [("issue", "Issue #43", issue_url)]


def test_issue_comments_add_session_link_without_event_row() -> None:
    issue_url = "https://github.com/o/r/issues/44"
    history = build_activity_history(
        start=START,
        end=END,
        members=_members(),
        records=[
            {
                "trace_id": "t-comment",
                "person_id": "alice",
                "timestamp": "2026-07-01T13:00:00+00:00",
                "source": "interactive",
                "kind": "event",
                "type": "github.issue_comment",
                "attributes": {
                    "github.action": "commented",
                    "github.kind": "issue",
                    "github.number": 44,
                    "github.repo": "o/r",
                    "github.url": issue_url,
                },
                "payload": {
                    "action": "commented",
                    "issue": {"number": 44, "html_url": issue_url},
                    "comment": {
                        "id": 1,
                        "html_url": f"{issue_url}#issuecomment-1",
                    },
                },
            }
        ],
    )

    assert history.events == []
    assert [(link.label, link.url) for link in history.sessions[0].links] == [
        ("Issue #44", issue_url)
    ]
