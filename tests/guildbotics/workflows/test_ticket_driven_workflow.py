import pytest

from guildbotics.entities.task import Task
from guildbotics.intelligences.common import AgentResponse, Labels
from guildbotics.workflows.ticket_driven_workflow import TicketDrivenWorkflow
from guildbotics.modes.mode_base import ModeBase


class StubTicketManager:
    """Simple async stub for ticket manager to record calls."""

    def __init__(self):
        self.moved = []  # list of (task, status)
        self.updated = []  # list of task
        self.commented = []  # list of (task, message)

    async def move_ticket(self, task: Task, status: str):
        self.moved.append((task, status))

    async def update_ticket(self, task: Task):
        self.updated.append(task)

    async def add_comment_to_ticket(self, task: Task, message: str):
        self.commented.append((task, message))


class StubContext:
    """Minimal context required by the workflow under test."""

    def __init__(self, task: Task, tm: StubTicketManager):
        self.task = task
        self._tm = tm
        self.team = object()  # not used when ModeBase.get_available_modes is mocked

    def get_ticket_manager(self):
        return self._tm

    def update_task(self, task: Task) -> None:
        self.task = task


@pytest.mark.asyncio
async def test_run_transitions_ready_to_in_progress_and_handles_asking(monkeypatch):
    # Arrange a task starting in READY with no role/mode
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager()
    ctx = StubContext(task, tm)

    # Mock role/mode identification
    async def fake_identify_role(context, input):
        return "dev"

    async def fake_identify_mode(context, available_modes, input):
        return "comment"

    monkeypatch.setattr(
        "guildbotics.workflows.ticket_driven_workflow.identify_role",
        fake_identify_role,
    )
    monkeypatch.setattr(
        "guildbotics.workflows.ticket_driven_workflow.identify_mode",
        fake_identify_mode,
    )
    # Avoid touching real team/services resolution
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.ModeBase.get_available_modes",
        lambda team: Labels({"comment": "desc"}),
    )

    # Mode.run returns ASKING and should cause early return after commenting
    class FakeMode:
        async def run(self, messages):
            return AgentResponse(status=AgentResponse.ASKING, message="need more info")

    monkeypatch.setattr(
        "guildbotics.modes.mode_base.ModeBase.get_mode", lambda context: FakeMode()
    )

    wf = TicketDrivenWorkflow(ctx)

    # Act
    await wf.run()

    # Assert READY -> IN_PROGRESS transition executed and persisted
    assert len(tm.moved) == 1 and tm.moved[0][1] == Task.IN_PROGRESS
    assert ctx.task.status == Task.IN_PROGRESS

    # Role and mode were identified, causing two update calls
    assert ctx.task.role == "dev" and ctx.task.mode == "comment"
    assert len(tm.updated) == 2

    # ASKING response triggers a comment but no move to IN_REVIEW
    assert len(tm.commented) == 1 and tm.commented[0][1] == "need more info"
    # still only one move (to IN_PROGRESS), not IN_REVIEW
    assert len(tm.moved) == 1


@pytest.mark.asyncio
async def test_run_completes_and_moves_in_progress_to_in_review(monkeypatch):
    # Arrange a task already IN_PROGRESS with role/mode set
    task = Task(
        id="2",
        title="T2",
        description="D2",
        role="dev",
        mode="comment",
        status=Task.IN_PROGRESS,
    )
    tm = StubTicketManager()
    ctx = StubContext(task, tm)

    # Mode.run returns DONE -> should comment and move to IN_REVIEW
    class FakeModeDone:
        async def run(self, messages):
            return AgentResponse(status=AgentResponse.DONE, message="done msg")

    monkeypatch.setattr(
        "guildbotics.modes.mode_base.ModeBase.get_mode", lambda context: FakeModeDone()
    )

    wf = TicketDrivenWorkflow(ctx)

    # Act
    await wf.run()

    # Assert comment added and status progressed to IN_REVIEW
    assert len(tm.commented) == 1 and tm.commented[0][1] == "done msg"
    assert len(tm.moved) == 1 and tm.moved[0][1] == Task.IN_REVIEW
    assert ctx.task.status == Task.IN_REVIEW
