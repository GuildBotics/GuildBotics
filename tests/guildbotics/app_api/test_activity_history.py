"""Session behaviour of the activity history normalizer.

A session is one execution described by its shared lifecycle record (a task
run or an interactive session record); the fact events recorded inside the
trace add its links, its title and the status layers above the lifecycle.
Chat-triggered workflows carry no title of their own, so the title must prefer
the PR / issue the session worked on, then the completion summary, then a
provider-neutral trigger label while the run is still in progress.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from guildbotics.app_api.activity_history import (
    ActivityLifecycle,
    build_activity_history,
    lifecycle_from_run,
    lifecycle_from_session,
)
from guildbotics.entities.task_run import TaskRunRecord, TaskRunResult
from guildbotics.entities.team import Person
from guildbotics.utils.i18n_tool import set_language, t

CHAT_TRIGGER_KEY = "observability.trace_title.chat_trigger"
START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 2, tzinfo=UTC)
TICKET_URL = "https://github.com/o/r/issues/42"
CHAT_ATTRIBUTES = {
    "service_run_id": "scheduler-run",
    "event.provider": "slack",
    "slack.channel": "C1",
    "slack.thread_ts": "100.1",
    "event_id": "E1",
}
TICKET_ATTRIBUTES = {
    "github.url": TICKET_URL,
    "github.number": "42",
    "github.kind": "issue",
}


def _lifecycle(
    trace_id: str = "t-chat",
    *,
    source: str = "event_listener",
    command: str = "workflows/chat_conversation_workflow",
    status: str = "running",
    started_at: str = "2026-07-01T10:00:00+00:00",
    ended_at: str = "",
    attributes: dict[str, Any] | None = None,
    summary: str = "",
    has_evidence: bool = True,
    person_id: str = "alice",
) -> ActivityLifecycle:
    return ActivityLifecycle(
        trace_id=trace_id,
        person_id=person_id,
        source=source,
        command=command,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        attributes=CHAT_ATTRIBUTES if attributes is None else attributes,
        summary=summary,
        has_evidence=has_evidence,
    )


def _ticket_lifecycle(**overrides: Any) -> ActivityLifecycle:
    values: dict[str, Any] = {
        "trace_id": "t-ticket",
        "source": "routine",
        "command": "workflows/ticket_driven_workflow",
        "started_at": "2026-07-01T11:00:00+00:00",
        "attributes": TICKET_ATTRIBUTES,
    }
    values.update(overrides)
    return _lifecycle(**values)


def _members() -> list[Person]:
    return [
        Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
    ]


def _event(
    event_type: str,
    timestamp: str,
    *,
    trace_id: str = "t-chat",
    payload: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
    kind: str = "event",
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "person_id": "alice",
        "timestamp": timestamp,
        "kind": kind,
        "type": event_type,
        "attributes": attributes or {},
        "payload": payload or {},
    }


def _history(
    lifecycles: list[ActivityLifecycle],
    records: list[dict[str, Any]] | None = None,
    members: list[Person] | None = None,
) -> Any:
    return build_activity_history(
        start=START,
        end=END,
        members=members or _members(),
        lifecycles=lifecycles,
        records=records or [],
    )


def _session(
    lifecycle: ActivityLifecycle, records: list[dict[str, Any]] | None = None
) -> Any:
    history = _history([lifecycle], records)
    assert len(history.sessions) == 1
    return history.sessions[0]


def _title(
    lifecycle: ActivityLifecycle, records: list[dict[str, Any]] | None = None
) -> str:
    return str(_session(lifecycle, records).title)


def test_completed_chat_session_titled_by_summary_first_line() -> None:
    title = _title(
        _lifecycle(status="succeeded", summary="請求プランの質問に回答\n詳細は省略")
    )
    assert title == "請求プランの質問に回答"


def test_in_progress_chat_session_uses_neutral_trigger_label() -> None:
    set_language("ja")
    assert _title(_lifecycle()) == t(CHAT_TRIGGER_KEY, provider="Slack")


@pytest.mark.parametrize(
    ("lifecycle_status", "expected"),
    [
        ("running", "running"),
        ("succeeded", "success"),
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("interrupted", "failed"),
        ("result_unknown", "info"),
    ],
)
def test_session_status_comes_from_the_lifecycle_record(
    lifecycle_status: str, expected: str
) -> None:
    # The command boundary events that used to carry this stay on the device
    # that ran the command; every device reads the same lifecycle record.
    assert _session(_lifecycle(status=lifecycle_status)).status == expected


def test_interactive_session_shows_its_last_commands_result() -> None:
    interactive = _lifecycle(
        "t-int", source="interactive", command="member git push", status="failed"
    )
    assert _session(interactive).status == "failed"
    assert _session(interactive).mode == "interactive"


def test_session_times_come_from_the_lifecycle_record() -> None:
    session = _session(
        _lifecycle(
            status="succeeded",
            started_at="2026-07-01T10:00:00+00:00",
            ended_at="2026-07-01T10:05:00+00:00",
        ),
        [_event("workflow.completed", "2026-07-01T10:04:00+00:00")],
    )
    assert session.started_at == "2026-07-01T10:00:00+00:00"
    assert session.ended_at == "2026-07-01T10:05:00+00:00"
    assert session.duration_seconds == 300.0


def test_running_session_ends_at_its_latest_fact_record() -> None:
    session = _session(
        _lifecycle(started_at="2026-07-01T10:00:00+00:00"),
        [_event("github.push", "2026-07-01T10:03:00+00:00", payload={"commits": []})],
    )
    assert session.ended_at == "2026-07-01T10:03:00+00:00"


def test_fact_records_without_a_lifecycle_make_events_but_no_session() -> None:
    pr_url = "https://github.com/o/r/pull/51"
    history = _history(
        [],
        [
            _event(
                "github.pull_request",
                "2026-07-01T12:01:00+00:00",
                trace_id="t-elsewhere",
                attributes={
                    "github.action": "opened",
                    "github.kind": "pull_request",
                    "github.number": 51,
                    "github.url": pr_url,
                },
                payload={"action": "opened", "pull_request": {"number": 51}},
            )
        ],
    )
    assert history.sessions == []
    assert [(event.type, event.url) for event in history.events] == [
        ("pr_create", pr_url)
    ]


@pytest.mark.parametrize("status", ["succeeded", "running"])
@pytest.mark.parametrize("source", ["scheduled", "routine", "event_listener", ""])
def test_quiet_automated_run_without_evidence_is_hidden(
    status: str, source: str
) -> None:
    # A scheduled command that ran and touched nothing is not activity. The
    # rule does not read the source: a run record written before the source
    # was recorded (an idle patrol of the past) hides the same way.
    history = _history(
        [
            _lifecycle(
                "t-cron",
                source=source,
                command="daily-report",
                status=status,
                attributes={},
                has_evidence=False,
            )
        ]
    )
    assert history.sessions == []


@pytest.mark.parametrize(
    "lifecycle",
    [
        _lifecycle("t-failed", source="scheduled", status="failed", has_evidence=False),
        _lifecycle("t-result", source="scheduled", status="succeeded", summary="ran"),
        _lifecycle("t-target", source="scheduled", attributes=TICKET_ATTRIBUTES),
    ],
    ids=["failed", "recorded an outcome", "names a target"],
)
def test_automated_run_that_failed_or_recorded_work_is_shown(
    lifecycle: ActivityLifecycle,
) -> None:
    assert len(_history([lifecycle]).sessions) == 1


def test_rate_limited_workflow_event_sets_session_rate_limit() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event(
                "workflow.rate_limited",
                "2026-07-01T10:00:00+00:00",
                attributes={
                    "rate_limit.retry_after_at": "2026-07-04T11:44:00+09:00",
                    "rate_limit.retry_after_text": "11:44 AM",
                },
                payload={
                    "retry_after_at": "fallback",
                    "retry_after_text": "fallback",
                },
            )
        ],
    )

    assert session.status == "rate_limited"
    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_at == "2026-07-04T11:44:00+09:00"
    assert session.rate_limit.retry_after_text == "11:44 AM"


def test_rate_limited_ticket_workflow_event_sets_session_rate_limit() -> None:
    session = _session(
        _ticket_lifecycle(status="failed", has_evidence=False),
        [
            _event(
                "workflow.rate_limited",
                "2026-07-01T11:00:00+00:00",
                trace_id="t-ticket",
                attributes={
                    "rate_limit.retry_after_at": "2026-07-05T12:00:00+09:00",
                    "rate_limit.retry_after_text": "12:00 PM",
                },
            )
        ],
    )

    assert session.status == "rate_limited"
    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_at == "2026-07-05T12:00:00+09:00"
    assert session.rate_limit.retry_after_text == "12:00 PM"


def test_latest_rate_limit_record_wins() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event(
                "workflow.rate_limited",
                "2026-07-01T10:00:00+00:00",
                attributes={"rate_limit.retry_after_text": "old"},
            ),
            _event(
                "workflow.rate_limited",
                "2026-07-01T10:05:00+00:00",
                payload={"retry_after_text": "new"},
            ),
        ],
    )

    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_text == "new"


def test_completed_ticket_session_titled_by_summary_keeps_issue_link() -> None:
    session = _session(
        _ticket_lifecycle(status="succeeded", summary="Issue #42 のバグを修正")
    )
    assert session.title == "Issue #42 のバグを修正"
    assert any(
        link.kind == "issue" and link.label == "Issue #42" for link in session.links
    )


def test_ticket_session_without_summary_falls_back_to_issue_link() -> None:
    assert _title(_ticket_lifecycle(status="succeeded")) == "Issue #42"


def test_ticket_title_recorded_in_the_lifecycle_names_the_session_everywhere() -> None:
    # The scheduler opens the run's trace with the ticket's attributes and the
    # boundary mirrors them into the run record, so a device that never ran
    # the ticket still titles the session.
    title = _title(
        _ticket_lifecycle(
            attributes={**TICKET_ATTRIBUTES, "github.title": "ログイン修正"}
        )
    )
    assert title == "ログイン修正"


def test_lifecycle_attributes_precede_fact_records_in_the_first_seen_merge() -> None:
    later = _work_target_record("")
    later["timestamp"] = "2026-07-01T11:30:00+00:00"
    later["trace_id"] = "t-ticket"
    title = _title(
        _ticket_lifecycle(attributes={**TICKET_ATTRIBUTES, "github.title": "元の課題"}),
        [later],
    )
    assert title == "元の課題"


def test_workflow_memory_write_links_back_to_owning_session() -> None:
    # The member CLI records into the workflow's trace (``join_trace``), so a
    # memory write it makes is the session's own record.
    memory_record = _event(
        "memory.update",
        "2026-07-01T10:01:00+00:00",
        kind="memory",
        attributes={
            "run_id": "task-run-1",
            "memory.action": "update",
            "memory.doc_id": "doc-xyz",
            "memory.path": "documents/personal/alice/doc-xyz",
        },
        payload={"title": "PR #244 の作業記録"},
    )
    session = _session(_lifecycle(), [memory_record])
    doc_links = [link for link in session.links if link.kind == "doc"]
    assert [link.label for link in doc_links] == ["PR #244 の作業記録"]
    assert "doc_id=doc-xyz" in doc_links[0].url


def test_read_only_memory_record_adds_no_link_and_no_title() -> None:
    # A `get`/`recall`/`touch` does not change a document, so it must not add a
    # link nor become the session title (its payload title is just what was read).
    read_record = _event(
        "memory.get",
        "2026-07-01T10:01:00+00:00",
        kind="memory",
        attributes={
            "run_id": "task-run-1",
            "memory.action": "get",
            "memory.doc_id": "read-doc",
            "memory.path": "documents/personal/alice/read-doc",
        },
        payload={"title": "読んだだけのメモ"},
    )
    set_language("ja")
    session = _session(_lifecycle(), [read_record])
    assert all(link.kind != "doc" for link in session.links)
    assert session.title == t(CHAT_TRIGGER_KEY, provider="Slack")


def _work_target_record(action: str) -> dict[str, Any]:
    return _event(
        "github.work_target",
        "2026-07-01T10:01:00+00:00",
        attributes={
            "github.action": action,
            "github.kind": "pull_request",
            "github.number": "528",
            "github.repo": "o/r",
            "github.url": "https://github.com/o/r/pull/528",
            "github.title": "Copilot の利用枠を表示する",
        },
        payload={"pull_request": {"number": 528}},
    )


def test_inspected_pull_request_titles_the_session_but_is_not_its_work() -> None:
    session = _session(_lifecycle(), [_work_target_record("inspected")])
    assert session.title == "Copilot の利用枠を表示する"
    assert session.links == []


def test_worked_pull_request_titles_and_links_the_session() -> None:
    session = _session(_lifecycle(), [_work_target_record("")])
    assert session.title == "Copilot の利用枠を表示する"
    assert [(link.kind, link.label) for link in session.links] == [
        ("pull_request", "PR #528")
    ]


@pytest.mark.parametrize("append_order", ["chronological", "reversed"])
def test_the_first_recorded_target_names_the_session_like_the_execution_list(
    append_order: str,
) -> None:
    # Both screens read attributes from the records in timestamp order: a
    # later, different target must not overtake the one the trace started on,
    # however the records were appended (they come from several processes).
    second = _work_target_record("")
    second["timestamp"] = "2026-07-01T10:02:00+00:00"
    second["attributes"] = {
        **second["attributes"],
        "github.kind": "issue",
        "github.number": "9",
        "github.url": "https://github.com/o/r/issues/9",
        "github.title": "A later, different item",
    }
    targets = [_work_target_record(""), second]
    if append_order == "reversed":
        targets.reverse()
    session = _session(_lifecycle(), targets)
    assert session.title == "Copilot の利用枠を表示する"
    assert [(link.kind, link.label) for link in session.links] == [
        ("pull_request", "PR #528"),
        ("issue", "Issue #9"),
    ]


def test_target_title_precedes_the_completion_summary() -> None:
    # The execution list and the activity timeline agree: the item worked on
    # names the trace, the member's summary only fills in when there is none.
    title = _title(
        _lifecycle(status="succeeded", summary="PR #528 をレビュー"),
        [_work_target_record("")],
    )
    assert title == "Copilot の利用枠を表示する"


def test_read_only_memory_title_is_skipped_for_session_title() -> None:
    # An interactive session that recalled memory then wrote a note must title
    # from the note it wrote, not the generic "Memory recall" of the read event.
    interactive = _lifecycle(
        "t-int", source="interactive", command="member chat reply", attributes={}
    )
    recall = _event(
        "memory.recall",
        "2026-07-01T10:01:00+00:00",
        trace_id="t-int",
        kind="memory",
        attributes={"memory.action": "recall"},
        payload={"title": "Memory recall"},
    )
    wrote = _event(
        "memory.update",
        "2026-07-01T10:02:00+00:00",
        trace_id="t-int",
        kind="memory",
        attributes={"memory.action": "update", "memory.doc_id": "d1"},
        payload={"title": "PR #247: レビュー対応"},
    )
    assert _title(interactive, [recall, wrote]) == "PR #247: レビュー対応"


def test_interactive_session_titles_from_its_recorded_target_or_command() -> None:
    plain = _lifecycle(
        "t-int", source="interactive", command="member chat reply", attributes={}
    )
    assert _title(plain) == "member chat reply"
    # The session record keeps the first ``github.*`` attributes its commands
    # recorded, so another device titles the session without the local
    # ``github.work_target`` event.
    targeted = _lifecycle(
        "t-int",
        source="interactive",
        command="member github pr inspect",
        attributes={"github.title": "利用枠の表示", "github.kind": "pull_request"},
    )
    assert _title(targeted) == "利用枠の表示"


def test_manual_command_session_is_hidden_but_its_events_remain() -> None:
    pr_url = "https://github.com/o/r/pull/51"
    history = _history(
        [_lifecycle("t-manual", source="manual", command="release-notes")],
        [
            _event(
                "github.pull_request",
                "2026-07-01T12:01:00+00:00",
                trace_id="t-manual",
                attributes={
                    "github.action": "opened",
                    "github.kind": "pull_request",
                    "github.number": 51,
                    "github.repo": "o/r",
                    "github.url": pr_url,
                },
                payload={
                    "action": "opened",
                    "pull_request": {
                        "number": 51,
                        "title": "Add release notes",
                        "html_url": pr_url,
                    },
                },
            )
        ],
    )

    assert history.sessions == []
    assert [(event.type, event.title, event.url) for event in history.events] == [
        ("pr_create", "PR #51 Created", pr_url)
    ]


def test_issue_create_event_and_comments_share_one_session_issue_link() -> None:
    issue_url = "https://github.com/o/r/issues/43"
    records = [
        _event(
            "github.issue",
            "2026-07-01T12:00:00+00:00",
            trace_id="t-issue",
            attributes={
                "github.action": "opened",
                "github.kind": "issue",
                "github.number": 43,
                "github.repo": "o/r",
                "github.url": issue_url,
            },
            payload={
                "action": "opened",
                "issue": {
                    "number": 43,
                    "title": "Track issue activity",
                    "html_url": issue_url,
                },
            },
        ),
        *[
            _event(
                "github.issue_comment",
                f"2026-07-01T12:0{index}:00+00:00",
                trace_id="t-issue",
                attributes={
                    "github.action": "commented",
                    "github.kind": "issue",
                    "github.number": 43,
                    "github.repo": "o/r",
                    "github.url": issue_url,
                },
                payload={
                    "action": "commented",
                    "issue": {"number": 43, "html_url": issue_url},
                    "comment": {
                        "id": index,
                        "html_url": f"{issue_url}#issuecomment-{index}",
                    },
                },
            )
            for index in (1, 2)
        ],
    ]

    history = _history(
        [
            _lifecycle(
                "t-issue",
                source="interactive",
                command="member github issue create",
                started_at="2026-07-01T12:00:00+00:00",
                attributes={},
            )
        ],
        records,
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
    history = _history(
        [
            _lifecycle(
                "t-comment",
                source="interactive",
                command="member github issue comment",
                started_at="2026-07-01T13:00:00+00:00",
                attributes={},
            )
        ],
        [
            _event(
                "github.issue_comment",
                "2026-07-01T13:00:00+00:00",
                trace_id="t-comment",
                attributes={
                    "github.action": "commented",
                    "github.kind": "issue",
                    "github.number": 44,
                    "github.repo": "o/r",
                    "github.url": issue_url,
                },
                payload={
                    "action": "commented",
                    "issue": {"number": 44, "html_url": issue_url},
                    "comment": {
                        "id": 1,
                        "html_url": f"{issue_url}#issuecomment-1",
                    },
                },
            )
        ],
    )

    assert history.events == []
    assert [(link.label, link.url) for link in history.sessions[0].links] == [
        ("Issue #44", issue_url)
    ]


def test_ticket_exhaustion_without_dispatch_event_shows_incomplete() -> None:
    # The ticket workflow shares the completion-missing event with the chat
    # dispatcher, but exhausts its attempt budget by posting an error comment
    # instead of scheduling a ``chat_dispatch`` retry. Without a dispatch
    # event, "missing" alone must not read as "retry_scheduled" (nothing is
    # actually retrying it).
    session = _session(
        _ticket_lifecycle(status="failed"),
        [
            _event(
                "workflow.completion_missing",
                "2026-07-01T11:00:11+00:00",
                trace_id="t-ticket",
                payload={"run_id": "run-1", "attempt": 3, "max_attempts": 3},
            )
        ],
    )
    assert session.status == "incomplete"


def test_missing_completion_alone_shows_incomplete_not_retry_scheduled() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event(
                "workflow.completion_missing",
                "2026-07-01T10:00:11+00:00",
                payload={"run_id": "run-1", "attempt": 1, "max_attempts": 2},
            )
        ],
    )
    assert session.status == "incomplete"


def test_scheduled_retry_shows_retry_scheduled() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event(
                "workflow.completion_missing",
                "2026-07-01T10:00:11+00:00",
                payload={"run_id": "run-1", "attempt": 2, "max_attempts": 2},
            ),
            _event(
                "chat_dispatch.retry_scheduled",
                "2026-07-01T10:00:12+00:00",
                payload={"next_attempt_at": "2026-07-01T10:05:00+00:00"},
            ),
        ],
    )
    assert session.status == "retry_scheduled"


def test_abandoned_dispatch_wins_over_lifecycle_success_and_rate_limit() -> None:
    session = _session(
        _lifecycle(status="succeeded"),
        [
            _event(
                "workflow.rate_limited",
                "2026-07-01T10:00:11+00:00",
                payload={"retry_after_at": "2026-07-01T12:00:00+00:00"},
            ),
            _event(
                "chat_dispatch.abandoned",
                "2026-07-01T10:00:12+00:00",
                payload={"run_id": "run-1"},
            ),
        ],
    )
    assert session.status == "abandoned"


def test_recorded_completion_shows_success_even_after_failed_attempt() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event("workflow.completion_missing", "2026-07-01T10:00:11+00:00"),
            _event("workflow.completed", "2026-07-01T10:00:12+00:00"),
        ],
    )
    assert session.status == "success"


def test_rate_limited_retry_keeps_rate_limit_status_and_details() -> None:
    session = _session(
        _lifecycle(status="failed"),
        [
            _event(
                "workflow.rate_limited",
                "2026-07-01T10:00:10+00:00",
                payload={"retry_after_at": "2026-07-01T12:00:00+00:00"},
            ),
            _event(
                "chat_dispatch.retry_scheduled",
                "2026-07-01T10:00:11+00:00",
                payload={"error_category": "rate_limited"},
            ),
        ],
    )
    assert session.status == "rate_limited"
    assert session.rate_limit is not None
    assert session.rate_limit.retry_after_at == "2026-07-01T12:00:00+00:00"


def _run_record(**overrides: Any) -> TaskRunRecord:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "work_kind": "workflows/chat_conversation_workflow",
        "execution_mode": "autonomous",
        "member_id": "alice",
        "device_id": "device-1",
        "source": "event_listener",
        "attributes": CHAT_ATTRIBUTES,
        "started_at": "2026-07-01T10:00:00+00:00",
    }
    values.update(overrides)
    return TaskRunRecord.model_validate(values)


def test_lifecycle_from_run_describes_the_run_record() -> None:
    lifecycle = lifecycle_from_run(
        _run_record(
            finished_at="2026-07-01T10:05:00+00:00",
            status="succeeded",
            safe_summary="答えた",
            result=TaskRunResult(
                subject_type="chat", subject_id="slack:C1:100.1:E1", status="done"
            ),
        )
    )
    assert lifecycle.trace_id == "run-1"
    assert lifecycle.person_id == "alice"
    assert lifecycle.source == "event_listener"
    assert lifecycle.command == "workflows/chat_conversation_workflow"
    assert lifecycle.status == "succeeded"
    assert lifecycle.ended_at == "2026-07-01T10:05:00+00:00"
    assert lifecycle.attributes == CHAT_ATTRIBUTES
    assert lifecycle.summary == "答えた"
    assert lifecycle.has_evidence is True


def test_lifecycle_from_run_keeps_the_boundary_summary_out_of_the_title() -> None:
    # ``finish`` writes "Execution completed." on every run; only a member's
    # recorded completion is a summary worth titling with.
    lifecycle = lifecycle_from_run(
        _run_record(status="succeeded", safe_summary="Execution completed.")
    )
    assert lifecycle.summary == ""
    assert lifecycle.has_evidence is False


def test_lifecycle_from_run_treats_user_initiated_runs_as_manual() -> None:
    lifecycle = lifecycle_from_run(
        _run_record(execution_mode="user_initiated", source="manual")
    )
    assert lifecycle.source == "manual"
    assert _history([lifecycle]).sessions == []


def test_lifecycle_from_session_describes_the_session_record() -> None:
    lifecycle = lifecycle_from_session(
        {
            "trace_id": "t-int",
            "person_id": "alice",
            "command": "member memory recall",
            "status": "success",
            "started_at": "2026-07-01T10:00:00+00:00",
            "last_seen_at": "2026-07-01T10:20:00+00:00",
            "attributes": {"github.title": "利用枠"},
        }
    )
    assert lifecycle.source == "interactive"
    assert lifecycle.ended_at == "2026-07-01T10:20:00+00:00"
    assert lifecycle.attributes == {"github.title": "利用枠"}
    assert _session(lifecycle).mode == "interactive"
