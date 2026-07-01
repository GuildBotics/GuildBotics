from datetime import UTC, datetime, timedelta

from guildbotics.observability.interactive_sessions import InteractiveTraceStore


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
