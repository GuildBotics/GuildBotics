import os
from pathlib import Path

import pytest

from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.entities.task import Task
from guildbotics.intelligences.common import AgentResponse
from guildbotics.templates.commands.workflows import ticket_driven_workflow
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.i18n_tool import get_language, set_language, t


@pytest.fixture(autouse=True)
def _isolated_workspace_data(monkeypatch, tmp_path):
    previous_language = get_language()
    set_language("en")
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / ".guildbotics" / "data"))
    yield
    set_language(previous_language)


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
        # When set, only the Nth agent turn records a completion so earlier turns
        # fail the gate and the workflow retries.
        self.complete_on_attempt = None
        self._invoke_calls = 0
        self.task_run_status = "done"
        self.evidence_type = "issue_comment"
        self.task_run_store_root = None
        self.data_dir_after_invoke = None

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
            (command_name, args, kwargs, kwargs.get("agent_execution_context"))
        )
        if isinstance(self.invoke_response, Exception):
            raise self.invoke_response
        self._invoke_calls += 1
        should_complete = self.complete_task_run and (
            self.complete_on_attempt is None
            or self._invoke_calls >= self.complete_on_attempt
        )
        if should_complete:
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
        if self.data_dir_after_invoke is not None:
            os.environ[GUILDBOTICS_DATA_DIR] = str(self.data_dir_after_invoke)
        return self.invoke_response


@pytest.mark.asyncio
async def test_run_delegates_ready_ticket_to_cli_agent_and_moves_to_working(
    tmp_path,
):
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
    command_name, args, kwargs, execution_context = ctx.invocations[0]
    assert command_name == "functions/handle_github_ticket"
    assert args == ()
    data_dir = Path(tmp_path) / ".guildbotics" / "data"
    assert execution_context == {
        "run_id": kwargs["workflow_run_id"],
        "workspace_data_root": str(data_dir),
        "work_kind": "ticket",
        "work_identity": "https://github.com/GuildBotics/GuildBotics/issues/1",
        "resume_policy": "fresh",
        "attempt": 1,
        "continuation_input": t(
            "commands.workflows.common.agent_continuation",
            run_id=kwargs["workflow_run_id"],
        ),
    }
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
    # The shared workflow envelope is injected from the single i18n source.
    assert "guildbotics_execution_mode=workflow" in kwargs["workflow_contract"]
    assert "guildbotics member context --person aiko" in kwargs["workflow_contract"]


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
    tmp_path, relative_store_root
):
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
async def test_run_reads_completion_from_invocation_data_root_if_env_changes(tmp_path):
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.data_dir_after_invoke = tmp_path / "stale" / "data"

    response = await ticket_driven_workflow.main(ctx)

    assert response == AgentResponse(
        status=AgentResponse.DONE,
        message="done",
        skip_ticket_comment=True,
    )
    assert tm.commented == []


@pytest.mark.asyncio
async def test_run_passes_pull_request_work_type():
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
async def test_asking_response_requires_comment_evidence():
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
async def test_agent_done_without_task_completion_is_not_success(monkeypatch):
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
    monkeypatch,
):
    from guildbotics.capabilities.completion_retry import CompletionRetryExhausted

    # One attempt so the failing agent run escalates immediately.
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", "1")
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = RuntimeError("codex failed: secret-token-123")

    async def fake_talk_as(context, text, role, attachments):
        return text

    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", fake_talk_as)

    # The failing agent run is bounded and re-raised (so the scheduler counts the
    # error), while the ticket comment stays a safe, reader-facing message.
    with pytest.raises(CompletionRetryExhausted):
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


@pytest.mark.asyncio
async def test_ticket_retries_with_continuation_until_completion(monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", "5")
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    # Completes only on the second (continuation) turn.
    ctx.complete_on_attempt = 2

    response = await ticket_driven_workflow.main(ctx)

    assert response.status == AgentResponse.DONE
    handle_calls = [
        kwargs
        for name, _args, kwargs, _env in ctx.invocations
        if name == "functions/handle_github_ticket"
    ]
    assert len(handle_calls) == 2
    # Both attempts reuse one run id and one stable ticket conversation key.
    assert len({kwargs["workflow_run_id"] for kwargs in handle_calls}) == 1
    conversation_keys = {
        kwargs["agent_execution_context"]["work_identity"] for kwargs in handle_calls
    }
    assert conversation_keys == {"https://github.com/GuildBotics/GuildBotics/issues/1"}
    assert [
        kwargs["agent_execution_context"]["resume_policy"] for kwargs in handle_calls
    ] == ["fresh", "auto"]
    # Completed within budget: no error comment, no give-up.
    assert tm.commented == []


@pytest.mark.asyncio
async def test_ticket_exhaustion_posts_error_comment_and_raises(monkeypatch):
    from guildbotics.capabilities.completion_retry import CompletionRetryExhausted

    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", "2")
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.complete_task_run = False  # the agent never records a completion

    with pytest.raises(CompletionRetryExhausted):
        await ticket_driven_workflow.main(ctx)

    handle_calls = [
        name
        for name, _args, _kwargs, _env in ctx.invocations
        if name == "functions/handle_github_ticket"
    ]
    assert len(handle_calls) == 2
    assert len(tm.commented) == 1


@pytest.mark.asyncio
async def test_ticket_driven_workflow_reads_from_invocation(monkeypatch):
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    task = Task(
        id="999",
        title="Payload Task",
        description="Selected via invocation payload",
        status=Task.READY,
    )
    tm = StubTicketManager(None)  # No task on manager
    ctx = StubContext(None, tm)

    inv = WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="aiko",
        source="routine",
        trigger_type="ticket",
        payload={"task": task.model_dump()},
    )
    ctx.shared_state = {WORKFLOW_INVOCATION_KEY: inv}

    response = await ticket_driven_workflow.main(ctx)

    assert response is not None
    assert response.status == AgentResponse.DONE
    assert ctx.task.id == "999"
    assert ctx.task.title == "Payload Task"
    assert len(tm.moved) == 1
    assert tm.moved[0][0].id == "999"


def _rate_limit_error():
    from guildbotics.intelligences.brains.cli_agent import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    return CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="rate limit",
            returncode=75,
            error_category="rate_limited",
            error_details={
                "retry_after_at": "2026-07-04T11:44:00+09:00",
                "retry_after_text": "11:44 AM",
            },
        ),
    )


def _capture_recorded_event(monkeypatch):
    recorded: dict = {}

    def fake_record(*args, **kwargs):
        recorded.clear()
        recorded.update(kwargs)

    monkeypatch.setattr(
        "guildbotics.capabilities.workflow_rate_limits.record_correlated_event",
        fake_record,
    )
    return recorded


@pytest.mark.asyncio
async def test_ticket_rate_limit_posts_status_comment_and_records_event(monkeypatch):
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = _rate_limit_error()
    recorded_kwargs = _capture_recorded_event(monkeypatch)

    response = await ticket_driven_workflow.main(ctx)

    assert response is not None
    assert response.status == AgentResponse.DONE
    assert response.skip_ticket_comment is True
    assert "Rate limited. Reset: 11:44 AM" in response.message

    assert len(tm.commented) == 1
    comment = tm.commented[0][1]
    assert "guildbotics-workflow-status-v1" in comment
    assert "workflow_error" in comment
    assert "rate_limited" in comment

    assert recorded_kwargs
    assert recorded_kwargs["event_type"] == "workflow.rate_limited"
    assert recorded_kwargs["default_source"] == "routine"
    assert (
        recorded_kwargs["attributes"]["rate_limit.retry_after_at"]
        == "2026-07-04T11:44:00+09:00"
    )
    assert recorded_kwargs["payload"]["retry_after_text"] == "11:44 AM"


@pytest.mark.asyncio
async def test_ticket_rate_limit_records_event_even_if_comment_post_fails(monkeypatch):
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)

    class FailingCommentTicketManager(StubTicketManager):
        async def add_comment_to_ticket(self, task: Task, message: str):
            raise RuntimeError("GitHub is down")

    tm = FailingCommentTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = _rate_limit_error()
    recorded_kwargs = _capture_recorded_event(monkeypatch)

    response = await ticket_driven_workflow.main(ctx)

    assert response is not None
    assert response.status == AgentResponse.DONE
    assert response.skip_ticket_comment is True

    assert recorded_kwargs
    assert recorded_kwargs["event_type"] == "workflow.rate_limited"
    assert recorded_kwargs["payload"]["retry_after_text"] == "11:44 AM"
