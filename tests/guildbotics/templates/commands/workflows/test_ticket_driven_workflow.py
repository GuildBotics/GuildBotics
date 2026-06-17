import os
from pathlib import Path

import pytest

from guildbotics.capabilities.task_runs import TASK_RUN_ENV, TaskRunStore
from guildbotics.entities.task import Task
from guildbotics.intelligences.common import AgentResponse
from guildbotics.templates.commands.workflows import ticket_driven_workflow
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR


class StubTicketManager:
    def __init__(self, task=None, move_succeeds=True):
        self.task = task
        self.moved = []
        self.commented = []
        self.move_succeeds = move_succeeds

    async def get_task_to_work_on(self):
        return self.task

    async def move_ticket(self, task: Task, status: str) -> bool:
        self.moved.append((task, status))
        return self.move_succeeds

    async def add_comment_to_ticket(self, task: Task, message: str):
        self.commented.append((task, message))

    async def get_ticket_url(self, task: Task, markdown: bool = True):
        issue_id = task.id or "1"
        url = f"https://github.com/GuildBotics/GuildBotics/issues/{issue_id}"
        return f"[{task.title}]({url})" if markdown else url


class StubLogger:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, *args):
        self.errors.append(args)

    def warning(self, *args):
        self.warnings.append(args)


class StubContext:
    def __init__(self, task: Task, tm: StubTicketManager):
        self.task = task
        self._tm = tm
        self.invocations = []
        self.invoke_response = AgentResponse(status=AgentResponse.DONE, message="done")
        self.complete_task_run = True
        self.task_run_status = "done"
        self.evidence_type = "issue_comment"
        self.task_run_store_root = None

        class _PersonStub:
            person_id = "aiko"

        self.person = _PersonStub()
        self.language_name = "English"
        self.logger = StubLogger()

    def get_ticket_manager(self):
        return self._tm

    def update_task(self, task: Task) -> None:
        self.task = task

    async def invoke(self, command_name: str, *args, **kwargs):
        self.invocations.append(
            (command_name, args, kwargs, kwargs.get("cli_agent_env"))
        )
        if isinstance(self.invoke_response, Exception):
            raise self.invoke_response
        if self.complete_task_run:
            run_id = kwargs["workflow_run_id"]
            store = TaskRunStore(self.task_run_store_root)
            store.append_evidence(
                run_id,
                self.evidence_type,
                {"url": kwargs["ticket_url"], "person_id": kwargs["person_id"]},
            )
            store.complete(
                run_id,
                self.task_run_status,
                "completed through member capability",
                kwargs["ticket_url"],
                kwargs["person_id"],
            )
        return self.invoke_response


@pytest.mark.asyncio
async def test_run_delegates_ready_ticket_to_cli_agent_and_moves_to_working(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)

    response = await ticket_driven_workflow.main(ctx)

    assert response == AgentResponse(
        status=AgentResponse.DONE,
        message="done",
        skip_ticket_comment=True,
    )
    assert tm.moved == [(task, Task.IN_PROGRESS)]
    assert ctx.task.status == Task.IN_PROGRESS
    assert tm.commented == []
    assert len(ctx.invocations) == 1
    command_name, args, kwargs, cli_agent_env = ctx.invocations[0]
    assert command_name == "functions/handle_github_ticket"
    assert args == ()
    # Run id is scoped to the agent subprocess via a per-invocation overlay,
    # not the process-global os.environ.
    assert cli_agent_env == {
        TASK_RUN_ENV: kwargs["workflow_run_id"],
        GUILDBOTICS_DATA_DIR: str(Path(tmp_path) / ".guildbotics" / "data"),
    }
    assert os.environ.get(TASK_RUN_ENV) is None
    assert kwargs["person_id"] == "aiko"
    assert kwargs["ticket_url"] == "https://github.com/GuildBotics/GuildBotics/issues/1"
    assert kwargs["pull_request_url"] == ""
    assert kwargs["work_type"] == "issue"
    assert kwargs["trigger_reason"] == ""
    # The workflow no longer reads/passes issue content; the agent inspects it.
    assert "issue_title" not in kwargs
    assert "issue_description" not in kwargs
    assert kwargs["language"] == "English"
    assert kwargs["member_workspace"] == str(
        Path(tmp_path) / ".guildbotics" / "data" / "workspaces" / "aiko"
    )
    assert kwargs["cwd"] == Path(kwargs["member_workspace"])
    # Issue trigger: prepare command has no --pr-url.
    assert kwargs["prepare_command"] == (
        "guildbotics member git prepare --person aiko "
        "--issue-url https://github.com/GuildBotics/GuildBotics/issues/1"
    )
    # The capability reference is no longer injected per-prompt; the agent reads
    # it from the mandatory `member context` call (the single source of truth).
    assert "github_capability_help" not in kwargs


@pytest.mark.asyncio
async def test_move_to_working_keeps_status_when_move_is_noop():
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager(task, move_succeeds=False)
    ctx = StubContext(task, tm)

    await ticket_driven_workflow._move_task_to_working_if_ready(ctx, tm)

    assert tm.moved == [(task, Task.IN_PROGRESS)]
    assert ctx.task.status == Task.READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_store_root",
    [
        Path(".guildbotics-data") / "task-runs",
        Path(".guildbotics") / "data" / "task-runs",
    ],
)
async def test_run_accepts_task_completion_written_inside_member_workspace(
    monkeypatch, tmp_path, relative_store_root
):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    member_workspace = tmp_path / ".guildbotics" / "data" / "workspaces" / "aiko"
    ctx.task_run_store_root = member_workspace / relative_store_root

    response = await ticket_driven_workflow.main(ctx)

    assert response == AgentResponse(
        status=AgentResponse.DONE,
        message="done",
        skip_ticket_comment=True,
    )
    assert tm.commented == []


@pytest.mark.asyncio
async def test_run_passes_pull_request_work_type(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(
        id="1",
        title="T",
        description="D",
        status=Task.IN_PROGRESS,
        pull_request_url="https://github.com/GuildBotics/GuildBotics/pull/2",
        trigger_reason="pull_request_review",
    )
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)

    await ticket_driven_workflow.main(ctx)

    kwargs = ctx.invocations[0][2]
    assert kwargs["pull_request_url"].endswith("/pull/2")
    assert kwargs["work_type"] == "pull_request_review"
    # PR review trigger: prepare command must include --pr-url so the agent
    # checks out the PR head branch instead of a fresh ticket/<n> branch.
    assert kwargs["prepare_command"].endswith(
        "--issue-url https://github.com/GuildBotics/GuildBotics/issues/1 "
        "--pr-url https://github.com/GuildBotics/GuildBotics/pull/2"
    )
    assert tm.commented == []


@pytest.mark.asyncio
async def test_run_returns_none_when_no_task():
    tm = StubTicketManager(None)
    ctx = StubContext(Task(id="0", title="T", description="D"), tm)

    result = await ticket_driven_workflow.main(ctx)

    assert result is None
    assert ctx.invocations == []
    assert tm.moved == []


@pytest.mark.asyncio
async def test_asking_response_requires_comment_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = AgentResponse(status=AgentResponse.ASKING, message="asked")
    ctx.task_run_status = "asking"
    ctx.evidence_type = "pr_reply"

    response = await ticket_driven_workflow.main(ctx)

    assert response == AgentResponse(
        status=AgentResponse.ASKING,
        message="asked",
        skip_ticket_comment=True,
    )
    assert tm.commented == []


@pytest.mark.asyncio
async def test_agent_done_without_task_completion_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.complete_task_run = False

    async def fake_talk_as(context, text, role, attachments):
        return text

    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", fake_talk_as)

    with pytest.raises(Exception, match="Task run"):
        await ticket_driven_workflow.main(ctx)

    assert len(tm.commented) == 1
    assert "Task run" not in tm.commented[0][1]


@pytest.mark.asyncio
async def test_run_posts_safe_error_message_without_leaking_details(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = RuntimeError("codex failed: secret-token-123")

    async def fake_talk_as(context, text, role, attachments):
        return text

    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", fake_talk_as)

    with pytest.raises(RuntimeError, match="secret-token-123"):
        await ticket_driven_workflow.main(ctx)

    assert len(tm.commented) == 1
    comment = tm.commented[0][1]
    assert comment.strip()
    assert "secret-token-123" not in comment
    assert "RuntimeError" not in comment
    assert ".log" not in comment


def test_ticket_trace_attributes_for_issue_and_pull_request():
    issue = Task(
        id="1",
        title="T",
        description="D",
        repository="repo",
        number=42,
        url="https://github.com/owner/repo/issues/42",
    )
    assert ticket_driven_workflow._ticket_trace_attributes(issue) == {
        "github.repo": "repo",
        "github.kind": "issue",
        "github.url": "https://github.com/owner/repo/issues/42",
        "github.number": "42",
    }

    pr = Task(
        id="2",
        title="T",
        description="D",
        repository="repo",
        pull_request_url="https://github.com/owner/repo/pull/7",
    )
    assert ticket_driven_workflow._ticket_trace_attributes(pr) == {
        "github.repo": "repo",
        "github.kind": "pull_request",
        "github.url": "https://github.com/owner/repo/pull/7",
        "github.number": "7",
    }
