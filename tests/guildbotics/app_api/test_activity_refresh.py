from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from guildbotics.app_api import runtime as runtime_module
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import DiagnosticsStore


def test_activity_refresh_forces_on_entry_and_throttles_normal_reads(
    monkeypatch, tmp_path
):
    runtime = AppRuntime(
        EventBus(), diagnostics_store=DiagnosticsStore(tmp_path / "diag.jsonl")
    )
    team = Team(project=Project(), members=[Person(person_id="aiko", name="Aiko")])
    calls: list[tuple[str, str]] = []

    def refresh(
        candidate: Team, _start: datetime, _end: datetime, _period: tuple[str, str]
    ):
        assert candidate is team
        calls.append(_period)

    monkeypatch.setattr(runtime, "_sync_activity_events", refresh)
    monkeypatch.setattr(runtime_module, "_completed_activity_weeks", lambda: set())
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: 0.0)

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("guildbotics.app_api.runtime.threading.Thread", ImmediateThread)
    start = datetime(2999, 7, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    runtime._refresh_activity_events(team, start, end, force=False)
    runtime._refresh_activity_events(team, start, end, force=False)
    runtime._refresh_activity_events(team, start, end, force=True)
    runtime._refresh_activity_events(team, start, end + timedelta(days=1), force=False)

    assert calls == [
        (start.isoformat(), end.isoformat()),
        (start.isoformat(), end.isoformat()),
        (start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ]


@pytest.mark.parametrize(
    ("sync_start", "sync_end"),
    [
        ("2026-07-06T00:00:00Z", None),
        (None, "2026-07-13T00:00:00Z"),
        ("not-a-date", "2026-07-13T00:00:00Z"),
        ("2026-07-13T00:00:00Z", "2026-07-06T00:00:00Z"),
    ],
)
def test_activity_history_rejects_invalid_sync_ranges(sync_start, sync_end, tmp_path):
    runtime = AppRuntime(
        EventBus(), diagnostics_store=DiagnosticsStore(tmp_path / "diag.jsonl")
    )

    with pytest.raises(AppApiError) as exc_info:
        runtime.get_activity_history(
            start="2026-07-10T00:00:00Z",
            end="2026-07-11T00:00:00Z",
            sync_start=sync_start,
            sync_end=sync_end,
        )

    assert exc_info.value.code == "invalid_activity_sync_range"


def _shared_event(
    events: ActivityEventStore,
    event_type: str,
    trace_id: str,
    *,
    timestamp: str = "2026-07-10T02:00:00+00:00",
    payload: dict[str, object] | None = None,
) -> None:
    events.record(
        {
            "type": event_type,
            "timestamp": timestamp,
            "trace_id": trace_id,
            "person_id": "alice",
            "source": "event_listener",
            "payload": payload or {},
        }
    )


def _runtime_with_team(monkeypatch, store: DiagnosticsStore) -> AppRuntime:
    runtime = AppRuntime(EventBus(store=store), diagnostics_store=store)
    team = Team(
        project=Project(),
        members=[Person(person_id="alice", name="Alice", person_type="agent")],
    )
    monkeypatch.setattr(runtime, "_get_context", lambda: SimpleNamespace(team=team))
    monkeypatch.setattr(runtime, "_refresh_activity_events", lambda *_a, **_k: None)
    return runtime


def test_activity_records_keep_the_trace_of_every_device(tmp_path):
    """A fact recorded on another device still belongs to its trace here: the
    execution is shared, and only its detail view is local."""
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        {
            "kind": "event",
            "type": "command.finished",
            "timestamp": "2026-07-10T01:00:00+00:00",
            "trace_id": "local-1",
            "person_id": "alice",
            "source": "interactive",
        }
    )
    events = ActivityEventStore()
    for trace_id in ("local-1", "remote-1"):
        _shared_event(
            events, "github.push", trace_id, payload={"ref": "refs/heads/main"}
        )
    runtime = AppRuntime(EventBus(store=store), diagnostics_store=store)

    records = runtime._activity_records_between(
        datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 11, tzinfo=UTC)
    )

    pushes = [item for item in records if item["type"] == "github.push"]
    assert sorted(str(item.get("trace_id")) for item in pushes) == [
        "local-1",
        "remote-1",
    ]


def test_history_shows_another_devices_run_whole_and_withholds_only_its_detail(
    monkeypatch, tmp_path
):
    """The run of another device carries its links and the status layer its
    facts add; what this device cannot offer is the detail view alone."""
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    runtime = _runtime_with_team(monkeypatch, store)
    runs = RunStore()
    now = datetime.now(UTC)
    for run_id in ("local-1", "remote-1"):
        runs.start_record(
            run_id,
            work_kind="chat",
            execution_mode="autonomous",
            member_id="alice",
            source="event_listener",
        )
        runs.finish_record(run_id, status="failed")
    # Only the local run left a transcript on this device.
    store.record(
        {
            "kind": "event",
            "type": "command.finished",
            "timestamp": now.isoformat(),
            "trace_id": "local-1",
            "person_id": "alice",
            "source": "event_listener",
        }
    )
    events = ActivityEventStore()
    for trace_id in ("local-1", "remote-1"):
        _shared_event(
            events,
            "github.push",
            trace_id,
            timestamp=now.isoformat(),
            payload={
                "ref": "refs/heads/feature",
                "commits": [
                    {
                        "id": "abc1234",
                        "message": "Wire it",
                        "url": "https://github.com/o/r/commit/abc1234",
                    }
                ],
            },
        )
    _shared_event(
        events,
        "chat_dispatch.abandoned",
        "remote-1",
        timestamp=(now + timedelta(seconds=1)).isoformat(),
        payload={"run_id": "remote-1"},
    )

    history = runtime.get_activity_history(
        start=(now - timedelta(days=1)).isoformat(),
        end=(now + timedelta(days=1)).isoformat(),
    )

    sessions = {session.trace_id: session for session in history.sessions}
    assert set(sessions) == {"local-1", "remote-1"}
    assert [link.kind for link in sessions["remote-1"].links] == ["commit"]
    assert sessions["remote-1"].status == "abandoned"
    assert sessions["remote-1"].detail_available is False
    assert [link.kind for link in sessions["local-1"].links] == ["commit"]
    assert sessions["local-1"].status == "failed"
    assert sessions["local-1"].detail_available is True


def test_activity_records_keep_the_whole_window_past_a_thousand_facts(tmp_path):
    """The old per-request cap kept the newest 1000 raw records, so a busy
    week lost its older days. Every fact in the window is returned."""
    events = ActivityEventStore()
    for index in range(1201):
        _shared_event(
            events,
            "github.push",
            f"t-{index}",
            timestamp=(
                datetime(2026, 7, 6, tzinfo=UTC) + timedelta(minutes=5 * index)
            ).isoformat(),
        )
    runtime = AppRuntime(
        EventBus(), diagnostics_store=DiagnosticsStore(tmp_path / "diag.jsonl")
    )

    records = runtime._activity_records_between(
        datetime(2026, 7, 6, tzinfo=UTC), datetime(2026, 7, 13, tzinfo=UTC)
    )

    assert len(records) == 1201
    assert {item["trace_id"] for item in records} >= {"t-0", "t-1200"}
