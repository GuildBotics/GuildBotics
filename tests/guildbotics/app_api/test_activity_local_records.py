"""Local provider work reaches both display APIs through the real writers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from guildbotics.app_api.activity_history import run_subject_id
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.capabilities.member_activity_events import (
    record_member_issue_create_event,
    record_member_pr_create_event,
    record_member_push_event,
    record_member_work_target,
)
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.observability import diagnostics_events
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.runtime.member_invocation import ChatSubject
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.app_api.owner import owner_client
from tests.guildbotics.local_code_host import local_member, pull_request, ticket


@pytest.mark.asyncio
async def test_local_targets_push_and_chat_reach_activity_and_execution_titles(
    monkeypatch, tmp_path
):
    member = local_member()
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    monkeypatch.setattr(diagnostics_events, "_STORE", store)
    runtime = AppRuntime(EventBus(store=store))
    monkeypatch.setattr(
        runtime, "_get_context", lambda: SimpleNamespace(team=member.team)
    )
    monkeypatch.setattr(runtime, "_refresh_activity_events", lambda *_a, **_k: None)
    runs = RunStore()
    targets = [
        (
            member.tickets,
            ticket(1),
            "ticket",
            "Ticket T-1",
            record_member_issue_create_event,
        ),
        (
            member.code,
            pull_request(3, "feature"),
            "pull_request",
            "PR !3",
            record_member_pr_create_event,
        ),
    ]
    for service, url, kind, label, record in targets:
        page = await service.read(
            "tickets" if kind == "ticket" else "pull_requests", url
        )
        assert page.target is not None
        target = {**page.target.model_dump(), "provider": "local"}
        with trace_scope("routine", person_id=member.person.person_id, trace_id=kind):
            runs.start_record(
                kind,
                work_kind="ticket",
                execution_mode="autonomous",
                member_id=member.person.person_id,
                source="routine",
            )
            record_member_work_target(member.person, target, read_only=False)
            record(member.person, {"target": target, "created": True})
            runs.finish_record(kind, status="succeeded")
        assert store.get_summary(kind)["title"] == target["title"]

    chat = ChatSubject("local", "channel-A", "thread-B", "event-C", "self")
    attrs = {
        "chat.provider": chat.service,
        "chat.channel": chat.channel_id,
        "chat.thread": chat.thread_id,
        "chat.message": "message-D",
        "event_id": chat.event_id,
    }
    assert run_subject_id(attrs) == chat.subject_id
    for run_id in ["push", "chat"]:
        with trace_scope(
            "event_listener",
            person_id=member.person.person_id,
            trace_id=run_id,
            attributes=attrs,
        ):
            runs.start_record(
                run_id,
                work_kind="chat",
                execution_mode="autonomous",
                member_id=member.person.person_id,
                source="event_listener",
                attributes=attrs,
            )
            if run_id == "push":
                record_member_push_event(
                    member.person,
                    {
                        "pushed": True,
                        "branch": "feature",
                        "commits": [
                            {
                                "id": "abc1234",
                                "message": "Local commit",
                                "url": "local://code/acme/team/demo/commits/abc1234",
                            }
                        ],
                    },
                    provider="local",
                )
            else:
                runs.append(
                    run_id,
                    {
                        "kind": "evidence",
                        "evidence_type": "chat_reply",
                        "payload": {
                            "channel_id": chat.channel_id,
                            "thread_id": chat.thread_id,
                        },
                    },
                )
                diagnostics_events.record_correlated_event(
                    event_type="workflow.completed", payload={}
                )
            runs.finish_record(run_id, status="succeeded")
    now = datetime.now(UTC)
    client = owner_client(create_app(runtime=runtime))
    response = client.get(
        "/activity/history",
        params={
            "start": (now - timedelta(days=1)).isoformat(),
            "end": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 200
    sessions = {s["trace_id"]: s for s in response.json()["sessions"]}
    for _, url, kind, label, _ in targets:
        assert sessions[kind]["title"] == store.get_summary(kind)["title"]
        assert [(l["kind"], l["label"], l["url"]) for l in sessions[kind]["links"]] == [
            (kind, label, url)
        ]
    assert [(l["kind"], l["label"]) for l in sessions["push"]["links"]] == [
        ("commit", "Local commit")
    ]
    assert sessions["chat"]["title"] == t(
        "observability.trace_title.chat_trigger", provider="Local"
    )
    assert store.get_summary("chat")["title"] == sessions["chat"]["title"]
