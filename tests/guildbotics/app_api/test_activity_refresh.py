from datetime import UTC, datetime, timedelta

import pytest

from guildbotics.app_api import runtime as runtime_module
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.entities.team import Person, Project, Team
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
