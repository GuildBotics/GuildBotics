from pathlib import Path

import httpx
import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.code_hosting_service import (
    InlineComment,
    InlineCommentThread,
    PullRequest,
    ReviewComment,
    ReviewComments,
)
from guildbotics.intelligences.common import (
    AgentResponse,
    ImprovementRecommendations,
    ImprovementSuggestion,
    RootCauseAnalysis,
    RootCauseItem,
)
from guildbotics.modes.edit_mode import EditMode


class StubGitTool:
    """Minimal stub for GitTool used by EditMode, with diff/commit control."""

    def __init__(
        self, workspace, repo_url, logger, user_name, user_email, default_branch
    ):
        self.workspace = workspace
        self.repo_url = repo_url
        self.logger = logger
        self.user_name = user_name
        self.user_email = user_email
        self.default_branch = default_branch
        self.repo_path = Path("/tmp/fake_repo")
        self._diff = ""
        self.commits = []

    def checkout_branch(self, branch_name: str):
        # No-op for tests
        pass

    def set_diff(self, diff: str):
        self._diff = diff

    def get_diff(self) -> str:
        return self._diff

    def commit_changes(self, message: str) -> str | None:
        # Record the message and return a fake SHA if non-empty diff was present
        self.commits.append(message)
        return "deadbeef" if self._diff else None


class StubCodeHostingService:
    def __init__(self):
        self.respond_calls: list[tuple[str, ReviewComments]] = []
        self.reaction_calls: list[tuple[str, int, str, bool]] = []
        self.created_pr: tuple[str, str, str, str] | None = None
        self._comments: ReviewComments | None = None
        self._pull_request: PullRequest | None = None

    def set_comments(self, rc: ReviewComments):
        self._comments = rc

    def set_pull_request(self, pr: PullRequest):
        self._pull_request = pr

    async def get_repository_url(self) -> str:
        return "https://example.com/org/repo.git"

    async def get_default_branch(self) -> str:
        return "main"

    async def get_pull_request_comments(
        self, html_url: str, include_all_comments: bool = False
    ) -> ReviewComments:
        assert html_url.startswith("https://example.com/pr/")
        assert include_all_comments is False
        assert self._comments is not None
        return self._comments

    async def respond_to_comments(self, html_url: str, comments: ReviewComments) -> None:
        self.respond_calls.append((html_url, comments))

    async def add_reaction_to_comment(
        self, html_url: str, comment_id: int, reaction: str, is_inline: bool
    ) -> None:
        self.reaction_calls.append((html_url, comment_id, reaction, is_inline))

    async def get_pull_request(self, html_url: str) -> PullRequest:
        assert self._pull_request is not None
        return self._pull_request

    async def create_pull_request(
        self, branch_name: str, title: str, description: str, ticket_url: str
    ) -> str:
        self.created_pr = (branch_name, title, description, ticket_url)
        return f"https://example.com/pr/999"


def stub_mode_env(monkeypatch, fake_context, code_hosting: StubCodeHostingService, *, diff: str = ""):
    """Common stubbing for GitTool, workspace path, and code hosting service."""
    # mode_base GitTool and workspace path
    monkeypatch.setattr("guildbotics.modes.mode_base.GitTool", StubGitTool)
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path",
        lambda person_id: Path("/tmp/fake_workspace"),
    )

    # inject our StubCodeHostingService
    def get_code_hosting_service(repository: str | None = None):
        return code_hosting

    fake_context.get_code_hosting_service = get_code_hosting_service  # type: ignore[attr-defined]

    # Create an EditMode and set its internal git_tool diff
    mode = EditMode(fake_context)
    # Precreate a stub git tool and attach it so get_git_tool/checkout return it
    stub = StubGitTool(
        workspace=Path("/tmp/fake_workspace"),
        repo_url="https://example.com/org/repo.git",
        logger=fake_context.logger,
        user_name=fake_context.person.account_info.get("git_user", "User"),
        user_email=fake_context.person.account_info.get("git_email", "u@example.com"),
        default_branch="main",
    )
    stub.set_diff(diff)
    mode.git_tool = stub  # type: ignore[attr-defined]

    # Ensure checkout returns our stub without side effects
    monkeypatch.setattr(EditMode, "checkout", lambda self: mode.get_git_tool())
    return mode


def set_task_pr_comment(fake_context: "FakeContext", url: str):
    fake_context.task.comments = [
        Message(
            content=f"Intro\n{Task.OUTPUT_PREFIX}[PR]({url})\nMore",
            author="assistant",
            author_type=Message.ASSISTANT,
            timestamp="",
        )
    ]


@pytest.mark.asyncio
async def test_retrospective_flow_creates_up_to_five_tickets_and_returns_asking(
    monkeypatch, fake_context
):
    # Arrange retrospective task and PR
    fake_context.task.status = Task.RETROSPECTIVE
    set_task_pr_comment(fake_context, "https://example.com/pr/123")

    ch = StubCodeHostingService()
    # PullRequest with simple comments
    rc = ReviewComments(review_comments=[], inline_comments=[])
    ch.set_pull_request(
        PullRequest(title="T", description="D", review_comments=rc, is_merged=True)
    )

    mode = stub_mode_env(monkeypatch, fake_context, ch)

    # Stub intelligences functions used in retrospective path
    async def fake_eval(context, pr_text):
        return "EVAL"
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.evaluate_interaction_performance",
        fake_eval,
    )

    rca = RootCauseAnalysis(
        items=[RootCauseItem(perspective="P", problem="p", root_cause="r", severity=0.9, severity_reason="sr")]
    )
    async def fake_rca(context, pr_text, evaluation):
        return rca
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.analyze_root_cause", fake_rca
    )

    # Prepare 7 suggestions to test trimming to 5
    suggestions = [
        ImprovementSuggestion(
            perspective="proc",
            proposal=f"Do {i}",
            rationale="why",
            implementation="how",
            impact_score=1 - i * 0.1,
            impact_reason="because",
        )
        for i in range(7)
    ]
    recs = ImprovementRecommendations(suggestions=suggestions)
    async def fake_recs(context, r):
        return recs
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.propose_process_improvements", fake_recs
    )

    # Ticket manager mock to capture created tasks
    created = {}

    class StubTicketManager:
        async def create_tickets(self, tasks: list[Task]):
            created["titles"] = [t.title for t in tasks]

    fake_context.get_ticket_manager = lambda: StubTicketManager()  # type: ignore[attr-defined]

    # Make i18n stable and talk_as deterministic
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)
    async def fake_talk_eval(context, topic, context_location, conversation_history):
        return "DISCUSS"
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.talk_as", fake_talk_eval
    )

    # Act
    res = await mode.run(messages=[Message(content="start", author="u", author_type=Message.USER)])

    # Assert: asking with combined message and only 5 tickets created
    assert res.status == AgentResponse.ASKING
    assert "DISCUSS" in res.message
    assert len(created.get("titles", [])) == 5


@pytest.mark.asyncio
async def test_review_no_threads_acknowledged_sets_no_message_and_responds(monkeypatch, fake_context):
    # Task set to in review and has PR URL
    fake_context.task.status = Task.IN_REVIEW
    set_task_pr_comment(fake_context, "https://example.com/pr/100")

    # Prepare comments: only review comments, last by reviewer
    comments = ReviewComments(
        review_comments=[
            ReviewComment(body="Looks good", author="Bob", created_at="2024-01-01T00:00:00Z", is_reviewee=False, comment_id=10)
        ],
        inline_comments=[],
    )
    ch = StubCodeHostingService()
    ch.set_comments(comments)

    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    # identify_pr_comment_action returns 'ack' so it should react and skip edits
    async def fake_identify(_, body):
        assert "Looks good" in body
        return "ack"

    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify
    )
    # edit_files should not be called, but provide a stub to fail if invoked
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.edit_files", lambda *a, **k: (_ for _ in ()).throw(AssertionError("edit_files should not run"))
    )

    # Stable translations and talk_as
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.talk_as", lambda *a, **k: "reply"
    )

    msgs = [Message(content="hi", author="bot", author_type=Message.ASSISTANT)]
    res = await mode.run(msgs)

    # Should respond to comments; message falls back to PR URL since no reply and no commit
    assert res.status == AgentResponse.DONE
    assert res.message == "https://example.com/pr/100"
    assert ch.respond_calls and ch.respond_calls[0][0] == "https://example.com/pr/100"
    # One reaction added (thumbs up) to the review comment, not inline
    assert ch.reaction_calls[0] == ("https://example.com/pr/100", 10, "+1", False)


@pytest.mark.asyncio
async def test_review_no_threads_edits_and_replies_with_commit_sha(monkeypatch, fake_context):
    fake_context.task.status = Task.IN_REVIEW
    set_task_pr_comment(fake_context, "https://example.com/pr/101")

    comments = ReviewComments(review_comments=[], inline_comments=[])
    ch = StubCodeHostingService()
    ch.set_comments(comments)

    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="some diff")

    # identify -> edit path
    async def fake_identify(_, body):
        return "edit"

    async def fake_edit(context, inputs, cwd):
        # Ensure repo path is passed
        assert cwd == Path("/tmp/fake_repo")
        return AgentResponse(status=AgentResponse.DONE, message="done edits")

    monkeypatch.setattr("guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify)
    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)

    # write_commit_message will be used because diff is present
    called = {"commit": 0, "talk": 0}

    async def fake_write_commit(context, task_title, changes):
        called["commit"] += 1
        assert "some diff" in changes
        return "commit msg"

    async def fake_talk_as(context, topic, context_location, conversation_history):
        called["talk"] += 1
        return "reply body"

    monkeypatch.setattr("guildbotics.modes.edit_mode.write_commit_message", fake_write_commit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.talk_as", fake_talk_as)
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)

    res = await mode.run([Message(content="start", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    # reply message should include commit sha added after reply body
    assert "deadbeef" in res.message
    assert called["commit"] == 1 and called["talk"] >= 1
    assert ch.respond_calls, "respond_to_comments should be called"


@pytest.mark.asyncio
async def test_review_inline_threads_mixed_ack_and_asking(monkeypatch, fake_context):
    fake_context.task.status = Task.IN_REVIEW
    set_task_pr_comment(fake_context, "https://example.com/pr/102")

    # Prepare three threads: one by reviewee (skip), one ack, one requires edit and asks
    t1 = InlineCommentThread(
        path="a.py",
        line=1,
        comments=[InlineComment(path="a.py", line=1, body="my last", comment_id=1, author="me", created_at="1", is_reviewee=True)],
    )
    t2 = InlineCommentThread(
        path="b.py",
        line=2,
        comments=[InlineComment(path="b.py", line=2, body="LGTM", comment_id=2, author="rv", created_at="1", is_reviewee=False)],
    )
    t3 = InlineCommentThread(
        path="c.py",
        line=3,
        comments=[InlineComment(path="c.py", line=3, body="fix it", comment_id=3, author="rv", created_at="1", is_reviewee=False)],
    )
    rc = ReviewComments(review_comments=[], inline_comments=[])
    rc.inline_comment_threads = [t1, t2, t3]

    ch = StubCodeHostingService()
    ch.set_comments(rc)
    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    async def fake_identify(_, body):
        return "ack" if body == "LGTM" else "edit"

    async def fake_edit(context, inputs, cwd):
        # For t3, return asking
        return AgentResponse(status=AgentResponse.ASKING, message="need info")

    async def fake_talk_as(context, topic, context_location, conversation_history):
        return "thread reply"

    monkeypatch.setattr("guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify)
    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.talk_as", fake_talk_as)
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)

    res = await mode.run([Message(content="msg", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    # Should have added one inline reaction (for t2)
    assert any(call[3] is True and call[1] == 2 for call in ch.reaction_calls)
    # thread 3 should have received a reply
    assert t3.reply == "thread reply"
    # No overall reply constructed (changed False), so message is PR URL
    assert res.message == "https://example.com/pr/102"


@pytest.mark.asyncio
async def test_nonreview_asking_returns_translated_question(monkeypatch, fake_context):
    fake_context.task.status = Task.IN_PROGRESS
    ch = StubCodeHostingService()
    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    # No PR URL in comments to force non-review path
    fake_context.task.comments = []

    async def fake_edit(context, inputs, cwd):
        return AgentResponse(status=AgentResponse.ASKING, message="raw question")

    async def fake_talk_as(context, topic, context_location, conversation_history):
        # Should be called with ticket_comment_context_location translation key
        assert context_location == "modes.edit_mode.ticket_comment_context_location"
        return "translated question"

    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.talk_as", fake_talk_as)
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)

    res = await mode.run([Message(content="need", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.ASKING
    assert res.message == "translated question"


@pytest.mark.asyncio
async def test_nonreview_commit_creates_pr_with_template(monkeypatch, fake_context, tmp_path):
    fake_context.task.status = Task.IN_PROGRESS
    ch = StubCodeHostingService()

    # Make workspace and a PR template file present
    tpl_dir = tmp_path / ".github"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "pull_request_template.md").write_text("TEMPLATE", encoding="utf-8")

    # Patch workspace path to our tmp
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path", lambda pid: tmp_path
    )
    # GitTool stub with non-empty diff => commit and push
    monkeypatch.setattr("guildbotics.modes.mode_base.GitTool", StubGitTool)

    def get_code_hosting_service(repository=None):
        return ch

    fake_context.get_code_hosting_service = get_code_hosting_service  # type: ignore[attr-defined]

    mode = EditMode(fake_context)

    async def fake_edit(context, inputs, cwd):
        # Ensure inputs come from messages_to_simple_dicts
        assert isinstance(inputs, list)
        return AgentResponse(status=AgentResponse.DONE, message="topic msg")

    async def fake_write_commit(context, task_title, changes):
        return "commit message"

    async def fake_write_pr_desc(context, changes, commit_message, ticket_url, pr_template):
        assert pr_template == "TEMPLATE"
        return "PR DESC"

    # Ticket manager for URL
    class StubTicketManager:
        async def get_ticket_url(self, task: Task) -> str:
            return "https://tickets/1"

    fake_context.get_ticket_manager = lambda: StubTicketManager()  # type: ignore[attr-defined]

    # Patch functions
    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.write_commit_message", fake_write_commit)
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.write_pull_request_description", fake_write_pr_desc
    )
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)
    # talk_as: used by get_done_response
    async def fake_talk_done(*a, **k):
        return "assistant summary"
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.talk_as", fake_talk_done
    )

    # Ensure there is a diff to trigger commit and PR creation
    gt = await mode.get_git_tool()
    gt.set_diff("diff")  # type: ignore[attr-defined]

    res = await mode.run([Message(content="go", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    assert "Output:" in res.message
    assert ch.created_pr is not None
    # Verify branch name uses task id
    assert ch.created_pr[0].startswith("ticket/")


@pytest.mark.asyncio
async def test_read_pull_request_template_default_when_missing(monkeypatch, fake_context, tmp_path):
    # No template files exist in workspace => should return default translation
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path", lambda pid: tmp_path
    )
    mode = EditMode(fake_context)
    # Translate function returns identifiable default
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.t",
        lambda key, **kw: "DEFAULT" if key == "modes.edit_mode.default_pr_template" else key,
    )
    assert mode.read_pull_request_template() == "DEFAULT"


@pytest.mark.asyncio
async def test_acknowledge_comment_variants(monkeypatch, fake_context):
    ch = StubCodeHostingService()

    def get_code_hosting_service(repository=None):
        return ch

    fake_context.get_code_hosting_service = get_code_hosting_service  # type: ignore[attr-defined]
    mode = EditMode(fake_context)

    # action != ack => False
    async def fake_identify_edit(*_):
        return "edit"
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify_edit
    )
    got = await mode._acknowledge_comment("https://example.com/pr/1", 1, "please fix")
    assert got is False

    # ack with no comment_id => True but no reaction
    async def fake_identify_ack(*_):
        return "ack"
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify_ack
    )
    got = await mode._acknowledge_comment("https://example.com/pr/1", None, "thanks")
    assert got is True and len(ch.reaction_calls) == 0

    # ack with comment_id => reaction attempted
    got = await mode._acknowledge_comment("https://example.com/pr/1", 42, "thanks")
    assert got is True and ch.reaction_calls[-1] == (
        "https://example.com/pr/1",
        42,
        "+1",
        False,
    )

    # Reaction raises but still returns True
    async def bad_add_reaction(*a, **k):
        raise httpx.HTTPError("network")

    monkeypatch.setattr(
        ch, "add_reaction_to_comment", bad_add_reaction
    )
    got = await mode._acknowledge_comment("https://example.com/pr/1", 99, "ok")
    assert got is True


@pytest.mark.asyncio
async def test_review_no_threads_edit_asking_empty_message_sets_default_question_called(
    monkeypatch, fake_context
):
    # Set IN_REVIEW with PR URL and no inline threads
    fake_context.task.status = Task.IN_REVIEW
    set_task_pr_comment(fake_context, "https://example.com/pr/200")

    comments = ReviewComments(review_comments=[], inline_comments=[])
    ch = StubCodeHostingService()
    ch.set_comments(comments)

    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    # Force edit path, and make edit_files ask without a message
    async def fake_identify(*_):
        return "edit"

    async def fake_edit(context, inputs, cwd):
        return AgentResponse(status=AgentResponse.ASKING, message="")

    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify
    )
    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)

    # Track that default_question translation key is requested
    called = {"default_question": 0}

    def fake_t(key, **kw):
        if key == "modes.edit_mode.default_question":
            called["default_question"] += 1
            return "DEFAULT_QUESTION"
        return key

    monkeypatch.setattr("guildbotics.modes.edit_mode.t", fake_t)
    # Stable talk_as
    monkeypatch.setattr(
        "guildbotics.modes.edit_mode.talk_as", lambda *a, **k: "reply"
    )

    res = await mode.run([Message(content="hi", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    # In review path with no changes => message falls back to PR URL
    assert res.message == "https://example.com/pr/200"
    # Ensure the default_question path executed
    assert called["default_question"] == 1


@pytest.mark.asyncio
async def test_review_inline_threads_edit_done_sets_changed_and_overall_reply(
    monkeypatch, fake_context
):
    fake_context.task.status = Task.IN_REVIEW
    set_task_pr_comment(fake_context, "https://example.com/pr/201")

    # One inline thread requiring edit and returning DONE
    t1 = InlineCommentThread(
        path="x.py",
        line=10,
        comments=[InlineComment(path="x.py", line=10, body="please fix", comment_id=5, author="rv", created_at="1", is_reviewee=False)],
    )
    rc = ReviewComments(review_comments=[], inline_comments=[])
    rc.inline_comment_threads = [t1]

    ch = StubCodeHostingService()
    ch.set_comments(rc)
    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    async def fake_identify(*_):
        return "edit"

    async def fake_edit(context, inputs, cwd):
        return AgentResponse(status=AgentResponse.DONE, message="thread ok")

    # t() should return a stable default message to trigger overall reply path
    def fake_t(key, **kw):
        if key == "modes.edit_mode.default_message":
            return "DEFAULT_MESSAGE"
        return key

    # talk_as should return different strings for thread vs overall
    async def fake_talk_as(context, topic, context_location, conversation_history):
        return "OVERALL_REPLY" if topic == "DEFAULT_MESSAGE" else "THREAD_REPLY"

    monkeypatch.setattr("guildbotics.modes.edit_mode.identify_pr_comment_action", fake_identify)
    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", fake_t)
    monkeypatch.setattr("guildbotics.modes.edit_mode.talk_as", fake_talk_as)

    res = await mode.run([Message(content="go", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    # Since changed=True and message exists, overall reply is used (not PR URL)
    assert res.message == "OVERALL_REPLY"
    # Ensure thread-level reply was also produced
    assert t1.reply == "THREAD_REPLY"


@pytest.mark.asyncio
async def test_nonreview_done_no_changes_returns_response_message(monkeypatch, fake_context):
    # Non-review path (no PR URL)
    fake_context.task.status = Task.IN_PROGRESS
    fake_context.task.comments = []
    ch = StubCodeHostingService()
    mode = stub_mode_env(monkeypatch, fake_context, ch, diff="")

    async def fake_edit(context, inputs, cwd):
        return AgentResponse(status=AgentResponse.DONE, message="topic result")

    monkeypatch.setattr("guildbotics.modes.edit_mode.edit_files", fake_edit)
    monkeypatch.setattr("guildbotics.modes.edit_mode.t", lambda key, **kw: key)

    res = await mode.run([Message(content="x", author="u", author_type=Message.USER)])
    assert res.status == AgentResponse.DONE
    assert res.message == "topic result"
    # Ensure skip_ticket_comment is False in this case
    assert getattr(res, "skip_ticket_comment", None) is False


@pytest.mark.asyncio
async def test_pr_to_text_appends_inline_comment_threads(monkeypatch, fake_context):
    # Build a PR with inline comment threads
    t1 = InlineCommentThread(
        path="a.py",
        line=1,
        comments=[InlineComment(path="a.py", line=1, body="c1", comment_id=1, author="rv", created_at="1", is_reviewee=False)],
    )
    rc = ReviewComments(review_comments=[], inline_comments=[])
    rc.inline_comment_threads = [t1]
    pr = PullRequest(title="T", description="D", review_comments=rc, is_merged=False)

    mode = EditMode(fake_context)

    # Provide deterministic translations focusing on inline thread appends
    def fake_t(key, **kw):
        if key == "modes.edit_mode.pull_request_text":
            return "PRTEXT|"
        if key == "modes.edit_mode.pull_request_inline_comment_thread":
            return f"THREAD|{kw['thread_number']}|{kw['thread_text']}|"
        if key == "modes.edit_mode.pull_request_merge_outcome":
            return f"MERGE|{kw['merge_outcome']}"
        return key

    monkeypatch.setattr("guildbotics.modes.edit_mode.t", fake_t)

    text = mode.pr_to_text(pr)
    # Should include inline thread section and merge outcome suffix
    assert "THREAD|1|" in text
    assert text.endswith("MERGE|closed")
