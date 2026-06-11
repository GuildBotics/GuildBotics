from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.entities.task import Task
from guildbotics.intelligences.common import GitHubTicketAgentResult
from guildbotics.templates.commands.workflows import ticket_driven_workflow


class StubTicketManager:
    """Simple async stub for ticket manager to record calls."""

    def __init__(self, task=None, move_succeeds=True):
        self.task = task
        self.moved = []
        self.commented = []
        self.created = []
        self.move_succeeds = move_succeeds

    async def get_task_to_work_on(self):
        return self.task

    async def move_ticket(self, task: Task, status: str) -> bool:
        self.moved.append((task, status))
        return self.move_succeeds

    async def add_comment_to_ticket(self, task: Task, message: str):
        self.commented.append((task, message))

    async def create_tickets(self, tasks: list[Task]):
        for index, task in enumerate(tasks, start=1):
            task.id = task.id or f"created-{index}"
        self.created.extend(tasks)

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
    """Minimal context required by the workflow under test."""

    def __init__(self, task: Task, tm: StubTicketManager):
        self.task = task
        self._tm = tm
        self.invocations = []
        self.code_hosting_service = StubCodeHostingService()
        self.invoke_response = GitHubTicketAgentResult(
            status=GitHubTicketAgentResult.DONE,
            summary="handled",
            commit_message="commit handled",
            pr_title="PR handled",
            pr_body="PR body",
            ticket_comment="Ticket handled",
        )

        class _PersonStub:
            person_id = "aiko"

        self.person = _PersonStub()
        self.language_name = "English"
        self.logger = StubLogger()

    def get_ticket_manager(self):
        return self._tm

    def update_task(self, task: Task) -> None:
        self.task = task

    def get_code_hosting_service(self, repository=None):
        return self.code_hosting_service

    async def invoke(self, command_name: str, *args, **kwargs):
        self.invocations.append((command_name, args, kwargs))
        if isinstance(self.invoke_response, Exception):
            raise self.invoke_response
        return self.invoke_response


class StubGitTool:
    repo_path = Path("/tmp/guildbotics-test-repo")

    def __init__(self, diff: str = ""):
        self.diff = diff
        self.checked_out = []
        self.commits = []
        self.repo = SimpleNamespace(
            head=SimpleNamespace(commit=SimpleNamespace(hexsha="head-before"))
        )

    def checkout_branch(self, branch_name: str):
        self.checked_out.append(branch_name)

    def get_diff(self):
        return self.diff

    def commit_changes(self, message: str):
        self.commits.append(message)
        return "abc123" if self.diff else None


class StubCodeHostingService:
    def __init__(self):
        self.pull_requests = []
        self.review_responses = []
        self.head_branch = "feature/pr-branch"
        self.inline_threads = [
            _thread(101),
            _thread(202),
        ]
        self.review_comments = SimpleNamespace(
            inline_comment_threads=self.inline_threads,
            reply=None,
            __str__=lambda self: "Review comment",
        )

    async def get_pull_request_head_branch(self, html_url: str):
        return self.head_branch

    async def get_pull_request_comments(self, html_url: str):
        return self.review_comments

    async def respond_to_comments(self, html_url: str, comments):
        inline_replies = [
            thread.reply for thread in comments.inline_comment_threads if thread.reply
        ]
        self.review_responses.append((html_url, comments.reply, inline_replies))

    async def create_pull_request(
        self, branch_name: str, title: str, description: str, ticket_url: str
    ):
        self.pull_requests.append((branch_name, title, description, ticket_url))
        return "https://github.com/GuildBotics/GuildBotics/pull/10"


def _thread(comment_id: int):
    return SimpleNamespace(
        reply=None,
        comments=[SimpleNamespace(comment_id=comment_id)],
    )


@pytest.mark.asyncio
async def test_run_delegates_ready_ticket_to_cli_agent_and_moves_to_working(
    monkeypatch,
):
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    git_tool = StubGitTool(diff="M README.md")

    async def fake_get_git_tool(context):
        assert context is ctx
        return git_tool

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )

    response = await ticket_driven_workflow.main(ctx)

    assert response and response.skip_ticket_comment is True
    assert tm.moved == [(task, Task.IN_PROGRESS)]
    assert ctx.task.status == Task.IN_PROGRESS
    assert git_tool.checked_out == ["ticket/1"]
    assert git_tool.commits == ["commit handled"]
    assert ctx.code_hosting_service.pull_requests == [
        (
            "ticket/1",
            "PR handled",
            "PR body",
            "https://github.com/GuildBotics/GuildBotics/issues/1",
        )
    ]
    assert len(tm.commented) == 1
    assert (
        "Output: [T](https://github.com/GuildBotics/GuildBotics/pull/10)"
        in (tm.commented[0][1])
    )
    assert len(ctx.invocations) == 1
    command_name, args, kwargs = ctx.invocations[0]
    assert command_name == "functions/handle_github_ticket"
    assert args == ()
    assert kwargs["ticket_url"] == "https://github.com/GuildBotics/GuildBotics/issues/1"
    assert kwargs["pull_request_url"] == ""
    assert kwargs["work_type"] == "issue"
    assert kwargs["trigger_reason"] == ""
    assert kwargs["issue_title"] == "T"
    assert kwargs["issue_description"] == "D"
    assert kwargs["issue_comments"] == "(none)"
    assert kwargs["review_context"] == ""
    assert "available_modes" not in kwargs
    assert kwargs["language"] == "English"
    assert kwargs["cwd"] == Path("/tmp/guildbotics-test-repo")


@pytest.mark.asyncio
async def test_move_to_working_keeps_status_when_move_is_noop():
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager(task, move_succeeds=False)
    ctx = StubContext(task, tm)

    await ticket_driven_workflow._move_task_to_working_if_ready(ctx, tm)

    # The move was attempted but did not take effect (no working lane), so the
    # in-memory status must stay aligned with the authoritative board state.
    assert tm.moved == [(task, Task.IN_PROGRESS)]
    assert ctx.task.status == Task.READY


@pytest.mark.asyncio
async def test_move_to_working_updates_status_when_move_succeeds():
    task = Task(id="1", title="T", description="D", status=Task.READY)
    tm = StubTicketManager(task, move_succeeds=True)
    ctx = StubContext(task, tm)

    await ticket_driven_workflow._move_task_to_working_if_ready(ctx, tm)

    assert ctx.task.status == Task.IN_PROGRESS


@pytest.mark.asyncio
async def test_run_passes_pull_request_url_without_mode_dispatch(monkeypatch):
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
    git_tool = StubGitTool(diff="M README.md")
    ctx.invoke_response = GitHubTicketAgentResult(
        status=GitHubTicketAgentResult.DONE,
        summary="review handled",
        commit_message="review commit",
        review_replies=[
            {"comment_id": 101, "reply": "Reply for first thread"},
            {"comment_id": 202, "reply": "Reply for second thread"},
        ],
    )

    async def fake_get_git_tool(context):
        return git_tool

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )

    await ticket_driven_workflow.main(ctx)

    assert tm.moved == []
    assert git_tool.checked_out == ["feature/pr-branch"]
    assert git_tool.commits == ["review commit"]
    assert ctx.code_hosting_service.review_responses == [
        (
            "https://github.com/GuildBotics/GuildBotics/pull/2",
            None,
            ["Reply for first thread", "Reply for second thread"],
        )
    ]
    assert ctx.invocations[0][0] == "functions/handle_github_ticket"
    assert ctx.invocations[0][2]["pull_request_url"].endswith("/pull/2")
    assert ctx.invocations[0][2]["work_type"] == "pull_request_review"


@pytest.mark.asyncio
async def test_run_posts_general_review_reply_to_pr_comment_with_warning(monkeypatch):
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
    ctx.invoke_response = GitHubTicketAgentResult(
        status=GitHubTicketAgentResult.DONE,
        summary="review handled",
        review_reply="General reply",
    )

    async def fake_get_git_tool(context):
        return StubGitTool()

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )

    await ticket_driven_workflow.main(ctx)

    assert ctx.code_hosting_service.review_responses == [
        (
            "https://github.com/GuildBotics/GuildBotics/pull/2",
            "General reply",
            [],
        )
    ]
    assert ctx.logger.warnings


@pytest.mark.asyncio
async def test_run_returns_none_when_no_task():
    tm = StubTicketManager(None)
    ctx = StubContext(Task(id="0", title="T", description="D"), tm)

    result = await ticket_driven_workflow.main(ctx)

    assert result is None
    assert ctx.invocations == []
    assert tm.moved == []


@pytest.mark.asyncio
async def test_run_posts_asking_response_to_ticket(monkeypatch):
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = GitHubTicketAgentResult(
        status=GitHubTicketAgentResult.ASKING,
        question="Need details",
        ticket_comment="CLI agent needs attention",
    )
    git_tool = StubGitTool()

    async def fake_get_git_tool(context):
        return git_tool

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )

    response = await ticket_driven_workflow.main(ctx)

    assert response is not None
    assert response.skip_ticket_comment is True
    assert tm.commented == [(task, "CLI agent needs attention")]


@pytest.mark.asyncio
async def test_run_creates_ticket_drafts_from_agent_result(monkeypatch):
    task = Task(
        id="1",
        title="T",
        description="D",
        status=Task.IN_PROGRESS,
        repository="repo",
        owner="alice",
    )
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = GitHubTicketAgentResult(
        status=GitHubTicketAgentResult.DONE,
        summary="no code change",
        ticket_comment="Created ticket drafts",
        new_tickets=[
            {
                "title": "Implement API",
                "description": "Build the requested API",
                "priority": 1,
                "inputs": ["original issue"],
                "output": "API implementation",
            }
        ],
    )
    git_tool = StubGitTool()

    async def fake_get_git_tool(context):
        return git_tool

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )

    await ticket_driven_workflow.main(ctx)

    assert len(tm.created) == 1
    created = tm.created[0]
    assert created.title == "Implement API"
    assert created.status == Task.READY
    # Drafts are not bound to a repository; otherwise get_ticket_url would try to
    # resolve a non-existent issue number from the draft's project-item id.
    assert created.repository is None
    assert created.owner == "alice"
    assert created.role is None
    assert created.mode is None
    assert "original issue" in created.description
    assert "API implementation" in created.description
    assert len(tm.commented) == 1
    assert "Created ticket drafts" in tm.commented[0][1]
    assert "[Implement API]" in tm.commented[0][1]


@pytest.mark.asyncio
async def test_run_posts_only_sanitized_error_log_path_to_ticket(monkeypatch, tmp_path):
    task = Task(id="1", title="T", description="D", status=Task.IN_PROGRESS)
    tm = StubTicketManager(task)
    ctx = StubContext(task, tm)
    ctx.invoke_response = RuntimeError("codex failed: secret-token-123")
    fake_home = tmp_path / "home"
    log_dir = fake_home / ".guildbotics" / "data" / "logs" / "ticket_driven_workflow"

    async def fake_get_git_tool(context):
        return StubGitTool()

    async def fake_talk_as(context, text, role, attachments):
        return text

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.ticket_driven_workflow.get_git_tool",
        fake_get_git_tool,
    )
    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", fake_talk_as)
    monkeypatch.setattr(ticket_driven_workflow.Path, "home", lambda: fake_home)
    monkeypatch.setattr(ticket_driven_workflow, "_error_log_dir", lambda: log_dir)

    with pytest.raises(RuntimeError, match="secret-token-123"):
        await ticket_driven_workflow.main(ctx)

    assert len(tm.commented) == 1
    comment = tm.commented[0][1]
    assert "secret-token-123" not in comment
    assert "RuntimeError" not in comment
    assert "~/.guildbotics/data/logs/ticket_driven_workflow/" in comment

    [log_file] = list(log_dir.glob("ticket_workflow_error_*.log"))
    assert "secret-token-123" in log_file.read_text(encoding="utf-8")
