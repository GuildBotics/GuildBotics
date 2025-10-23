import asyncio
from types import SimpleNamespace
from typing import List

import pytest

from guildbotics.drivers.utils import run_workflow
from guildbotics.entities import Task


class StubLogger:
    """Minimal logger capturing info/error messages for assertions."""

    def __init__(self) -> None:
        self.infos: List[str] = []
        self.errors: List[str] = []

    def info(self, msg: str) -> None:  # pragma: no cover - trivial
        self.infos.append(str(msg))

    def error(self, msg: str) -> None:  # pragma: no cover - trivial
        self.errors.append(str(msg))


class FakeContext:
    """Lightweight Context stub with only members used by run_workflow."""

    def __init__(self, person_id: str = "p1") -> None:
        self.logger = StubLogger()
        self.person = SimpleNamespace(person_id=person_id)
        self.task: Task | None = None

    def update_task(self, task: Task) -> None:
        self.task = task


@pytest.mark.asyncio
async def test_run_workflow_success_logs_and_returns_true(monkeypatch):
    class FakeCommandRunner:
        def __init__(self, context, workflow, args):
            self.context = context
            self.workflow = workflow
            self.args = args

        async def run(self):
            # Simulate successful workflow execution
            await asyncio.sleep(0)

    monkeypatch.setattr("guildbotics.drivers.utils.CommandRunner", FakeCommandRunner)

    ctx = FakeContext()
    task = Task(title="Title", description="Desc", workflow="any")
    ok = await run_workflow(ctx, task, task_type="scheduled")
    assert ok is True
    # Validate logs contain start and finish messages
    start_logs = [m for m in ctx.logger.infos if "Running scheduled task 'Title'" in m]
    finish_logs = [
        m for m in ctx.logger.infos if "Finished running scheduled task 'Title'" in m
    ]
    assert start_logs, "Start log not found"
    assert finish_logs, "Finish log not found"


@pytest.mark.asyncio
async def test_run_workflow_exception_logs_and_returns_false(monkeypatch):
    class FakeCommandRunnerError:
        def __init__(self, context, workflow, args):
            self.context = context
            self.workflow = workflow
            self.args = args

        async def run(self):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "guildbotics.drivers.utils.CommandRunner", FakeCommandRunnerError
    )

    ctx = FakeContext()
    task = Task(title="Failing", description="Desc", workflow="any")
    ok = await run_workflow(ctx, task, task_type="scheduled")
    assert ok is False
    # Validate error summary and traceback were logged
    error_summary = [
        e for e in ctx.logger.errors if "Error running workflow for task 'Failing'" in e
    ]
    assert error_summary, "Error summary log not found"
    traceback_logs = [e for e in ctx.logger.errors if "RuntimeError: boom" in e]
    assert traceback_logs, "Traceback log not found"
