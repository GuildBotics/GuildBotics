import asyncio
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from guildbotics.drivers.utils import _to_workflow, run_workflow
from guildbotics.entities import Task
from guildbotics.workflows import WorkflowBase


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


class SuccessWorkflow(WorkflowBase):
    """Workflow that records execution without raising errors."""

    def __init__(self, context: Any):
        super().__init__(context)
        self.ran = False

    async def run(self) -> None:
        # Simulate async work
        await asyncio.sleep(0)
        self.ran = True


class ErrorWorkflow(WorkflowBase):
    """Workflow that raises an exception during run()."""

    def __init__(self, context: Any):  # pragma: no cover - trivial
        super().__init__(context)

    async def run(self) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")


def test__to_workflow_converts_snake_to_pascal_and_module_path(monkeypatch):
    calls: List[Tuple[str, Any]] = []

    def fake_instantiate(module_and_cls: str, expected_type=None, **kwargs):
        calls.append((module_and_cls, kwargs))
        return "SENTINEL"

    monkeypatch.setattr("guildbotics.drivers.utils.instantiate_class", fake_instantiate)

    task = Task(title="t", description="d", workflow="ticket_driven")
    result = _to_workflow(context=object(), task=task)

    assert result == "SENTINEL"
    assert calls, "instantiate_class was not called"
    called_path, kwargs = calls[0]
    assert (
        called_path
        == "guildbotics.workflows.ticket_driven_workflow.TicketDrivenWorkflow"
    )
    # Ensure context is passed through
    assert "context" in kwargs


def test__to_workflow_accepts_fully_qualified_name(monkeypatch):
    calls: List[str] = []

    def fake_instantiate(module_and_cls: str, expected_type=None, **kwargs):
        calls.append(module_and_cls)
        return "OK"

    monkeypatch.setattr("guildbotics.drivers.utils.instantiate_class", fake_instantiate)

    fqcn = "some.pkg.Module.ClassName"
    task = Task(title="t", description="d", workflow=fqcn)
    result = _to_workflow(context=object(), task=task)
    assert result == "OK"
    assert calls == [fqcn]


@pytest.mark.asyncio
async def test_run_workflow_success_logs_and_returns_true(monkeypatch):
    def fake_instantiate(module_and_cls: str, expected_type=None, **kwargs):
        # Emulate instantiate_class by returning a proper WorkflowBase subclass instance
        return SuccessWorkflow(context=kwargs.get("context"))

    monkeypatch.setattr("guildbotics.drivers.utils.instantiate_class", fake_instantiate)

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
    def fake_instantiate(module_and_cls: str, expected_type=None, **kwargs):
        return ErrorWorkflow(context=kwargs.get("context"))

    monkeypatch.setattr("guildbotics.drivers.utils.instantiate_class", fake_instantiate)

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
