import json
from datetime import UTC, datetime, timedelta

from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
    InteractiveTraceStore,
)
from guildbotics.utils.workspace_sync_port import SHARED_RECORD_SCHEMA_VERSION


def test_interactive_trace_store_reuses_session_until_idle_timeout(tmp_path):
    store = InteractiveTraceStore(
        tmp_path / "interactive_trace_state.json",
        idle_timeout=timedelta(minutes=30),
    )
    first = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
        now=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
    )
    second = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
        now=datetime(2026, 7, 1, 10, 29, tzinfo=UTC),
    )
    third = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
        now=datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
    )

    assert second.trace_id == first.trace_id
    assert second.last_seen_at == "2026-07-01T10:29:00+00:00"
    assert third.trace_id != first.trace_id


def test_interactive_trace_store_separates_threads(tmp_path):
    store = InteractiveTraceStore(tmp_path / "interactive_trace_state.json")

    first = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
    )
    second = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-2",
    )

    assert second.trace_id != first.trace_id


def test_interactive_trace_store_prunes_expired_sessions(tmp_path):
    path = tmp_path / "interactive_trace_state.json"
    store = InteractiveTraceStore(path, idle_timeout=timedelta(minutes=30))
    first = store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
        now=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
    )

    store.start_or_touch(
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-2",
        now=datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
    )

    sessions = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    assert len(sessions) == 1
    assert first.trace_id not in str(sessions)


def _session(
    trace_id: str = "trace-1", started_at: str = "2026-07-01T10:00:00+00:00"
) -> InteractiveTraceSession:
    return InteractiveTraceSession(
        trace_id=trace_id,
        person_id="aiko",
        workspace="/repo",
        host="codex",
        thread_key="thread-1",
        started_at=started_at,
        last_seen_at="2026-07-01T10:00:00+00:00",
        expires_at="2026-07-01T10:30:00+00:00",
    )


def test_session_store_keeps_one_record_per_session(tmp_path):
    store = InteractiveSessionStore(tmp_path / "sessions")
    session = _session()

    store.record(
        session,
        command="member github pr inspect",
        status="success",
        attributes={
            "github.title": "first target",
            "github.kind": "pull_request",
            "interactive.workspace": "/repo",
        },
        now=datetime(2026, 7, 1, 10, 1, tzinfo=UTC),
    )
    record = store.record(
        session,
        command="member git push",
        status="failed",
        attributes={"github.title": "second target", "github.number": "5"},
        now=datetime(2026, 7, 1, 10, 9, tzinfo=UTC),
    )

    assert sorted(path.name for path in (tmp_path / "sessions").iterdir()) == [
        "trace-1.json"
    ]
    assert json.loads((tmp_path / "sessions/trace-1.json").read_text()) == record
    assert record["schema_version"] == SHARED_RECORD_SCHEMA_VERSION
    assert record["trace_id"] == "trace-1"
    assert record["person_id"] == "aiko"
    assert record["source"] == "interactive"
    # The first command names the session; the last command's result is its
    # status; the first value of each target attribute is kept.
    assert record["command"] == "member github pr inspect"
    assert record["status"] == "failed"
    assert record["started_at"] == "2026-07-01T10:00:00+00:00"
    assert record["last_seen_at"] == "2026-07-01T10:09:00+00:00"
    assert record["attributes"] == {
        "github.title": "first target",
        "github.kind": "pull_request",
        "github.number": "5",
    }


def test_session_store_lists_sessions_active_in_the_window(tmp_path):
    store = InteractiveSessionStore(tmp_path / "sessions")
    store.record(
        _session("early"),
        command="c",
        status="success",
        now=datetime(2026, 7, 1, 10, 5, tzinfo=UTC),
    )
    store.record(
        _session("late", started_at="2026-07-01T11:30:00+00:00"),
        command="c",
        status="success",
        now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )
    (tmp_path / "sessions/broken.json").write_text("{", encoding="utf-8")

    def _found(start: datetime, end: datetime) -> list[str]:
        return [item["trace_id"] for item in store.list_between(start, end)]

    assert _found(
        datetime(2026, 7, 1, 10, 2, tzinfo=UTC), datetime(2026, 7, 1, 11, tzinfo=UTC)
    ) == ["early"]
    assert _found(
        datetime(2026, 7, 1, 11, tzinfo=UTC), datetime(2026, 7, 1, 13, tzinfo=UTC)
    ) == ["late"]
    assert (
        _found(
            datetime(2026, 7, 1, 10, 6, tzinfo=UTC),
            datetime(2026, 7, 1, 10, 7, tzinfo=UTC),
        )
        == []
    )
