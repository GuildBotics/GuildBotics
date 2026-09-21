"""Every shared lifecycle writer masks and bounds what it records.

The shared state keeps two guarantees whatever writes it: a known secret value
never lands in the synchronized history, and no text lands unbounded. Task-run
records and interactive session records are the lifecycle writers, and the
attributes they mirror from a trace -- a PR title, a repository name -- are
free text like any error message. The writers are enumerated here so a new
lifecycle record kind has to say how it crosses the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
)
from guildbotics.utils.secret_store import KeyringSecretStore

SECRET = "sk-live-secret-12345"
TITLE = f"leaked {SECRET} in title"
LONG = "x" * 1000


def _write_task_run(workspace: Path) -> Path:
    RunStore().start_record(
        "run-1",
        work_kind="demo",
        execution_mode="autonomous",
        member_id="aiko",
        attributes={"github.title": TITLE, "github.repo": LONG},
    )
    return workspace / ".guildbotics/state/task-runs/run-1/result.json"


def _write_interactive_session(workspace: Path) -> Path:
    InteractiveSessionStore().record(
        InteractiveTraceSession(
            trace_id="trace-1",
            person_id="aiko",
            workspace=str(workspace),
            host="codex",
            thread_key="thread-1",
            started_at="2026-07-01T10:00:00+00:00",
            last_seen_at="2026-07-01T10:00:00+00:00",
            expires_at="2026-07-01T10:30:00+00:00",
        ),
        command="member github pr inspect",
        status="success",
        attributes={"github.title": TITLE, "github.repo": LONG},
    )
    return workspace / ".guildbotics/state/sessions/trace-1.json"


#: One writer per shared lifecycle record kind.
LIFECYCLE_WRITERS: dict[str, Callable[[Path], Path]] = {
    "task run": _write_task_run,
    "interactive session": _write_interactive_session,
}


@pytest.mark.parametrize(
    "writer", sorted(LIFECYCLE_WRITERS), ids=sorted(LIFECYCLE_WRITERS)
)
def test_lifecycle_writer_masks_secrets_and_bounds_text(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring
) -> None:
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    KeyringSecretStore(tmp_path / ".guildbotics" / "config").set(
        "OPENAI_API_KEY", SECRET
    )
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    path = LIFECYCLE_WRITERS[writer](tmp_path)

    text = path.read_text(encoding="utf-8")
    assert SECRET not in text
    attributes = json.loads(text)["attributes"]
    assert attributes["github.title"] == "leaked *** in title"
    assert len(attributes["github.repo"]) <= 500
