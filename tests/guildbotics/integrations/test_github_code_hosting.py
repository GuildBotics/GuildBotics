"""GitHub as a member's code host: its pull requests, their reviews and CI, the
git remote, and the member commands that write through it."""

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import httpx
import pytest

from guildbotics.capabilities import member_repository
from guildbotics.capabilities.artifact_archive import MAX_ARTIFACT_BYTES
from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.capabilities.member_context import member_context
from guildbotics.entities.team import Person, Project, Role, Team
from guildbotics.integrations.github.api import GitHubItem
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.issue_links import (
    append_external_issue_link,
    append_issue_link,
    preserve_issue_links,
    remove_issue_links,
)
from guildbotics.integrations.github.repository_scope import (
    ADD_PROJECT_ITEM,
    ADD_REACTION,
    NODE_REPOSITORY,
)
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
)
from guildbotics.runtime.code_hosting_service import GitCredential, PullRequestHead
from guildbotics.runtime.member_invocation import (
    GuestProcessError,
    GuestResult,
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import WorkTarget
from guildbotics.utils.fileio import host_temporary_root
from guildbotics.utils.i18n_tool import set_language, t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT
from tests.guildbotics.integrations.github_services import (
    API,
    LOGIN,
    OWNER,
    REPO,
    WEB,
    ScriptedGitHub,
    code_host,
    issue_url,
    member,
    pages,
    pull_url,
    ticket_service,
)
from tests.guildbotics.member_services import GuestDouble, MemberServices

PULL = pull_url(7)
ISSUE = issue_url(42)
_REPO_API = f"{API}/repos/{REPO}"


@pytest.mark.asyncio
async def test_git_credential_uses_the_members_token_and_github_username(monkeypatch):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "member-token")
    service = code_host(ScriptedGitHub(), monkeypatch)

    assert await service.git_credential() == GitCredential(
        username="x-access-token", password="member-token"
    )


def test_ci_download_limits_keep_artifacts_bounded_outside_the_stream_boundary():
    assert MAX_ARTIFACT_BYTES == 100 * 1024 * 1024
    assert MAX_ARTIFACT_BYTES > STREAM_READ_LIMIT


def _pull(number: int = 7, **fields: Any) -> dict[str, Any]:
    """Pull request ``number`` as GitHub answers it, at head ``abc123``."""
    return {
        "number": number,
        "node_id": f"PR_{number}",
        "title": f"pr {number}",
        "body": "",
        "state": "open",
        "html_url": pull_url(number),
        "user": {"login": "human"},
        "head": {"sha": "abc123", "ref": "feature", "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
        "mergeable_state": "clean",
        **fields,
    }


def _issue(number: int = 42, **fields: Any) -> dict[str, Any]:
    return {
        "number": number,
        "node_id": f"I_{number}",
        "title": f"issue {number}",
        "body": "",
        "state": "open",
        "html_url": issue_url(number),
        "user": {"login": "human"},
        "labels": [],
        **fields,
    }


def _written(number: int, url: str) -> dict[str, Any]:
    """What GitHub answers a comment, review, or reply that it made."""
    return {
        "id": number,
        "html_url": url,
        "created_at": "2026-01-01T00:00:00Z",
        "user": {"login": LOGIN},
    }


def _github() -> ScriptedGitHub:
    """Pull request #7 and issue #42 of the repository, both open."""
    return (
        ScriptedGitHub()
        .on("GET", f"/repos/{REPO}/pulls/7", _pull())
        .on("GET", f"/repos/{REPO}/issues/42", _issue())
        .on("GET", f"/repos/{REPO}", {"default_branch": "main"})
        .on("GET", f"/repos/{REPO}/pulls", [])
        .on("GET", f"/repos/{REPO}/labels", pages([{"name": "bug"}]))
    )


def _service(github: ScriptedGitHub, monkeypatch, **account: str):
    return code_host(github, monkeypatch, member(**account))


def _member(service: GitHubCodeHostingService, tickets=None) -> MemberServices:
    """The member's capabilities over ``service`` (and ``tickets``)."""
    return MemberServices(service.person, service.team, service, tickets)


def _target(url: str = PULL, title: str = "pr 7") -> WorkTarget:
    kind = "pull_request" if "/pull/" in url else "ticket"
    return WorkTarget(kind=kind, url=url, title=title, label=f"#{url.rsplit('/')[-1]}")


def _sequence(*answers: Any):
    """A route answering ``answers`` in turn, then the last one again."""
    remaining = list(answers)

    def answer(_request: httpx.Request) -> Any:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return answer


# --------------------------------------------------------------------------- #
#   Writes are read for before, never after                                   #
# --------------------------------------------------------------------------- #


def _writers(github: ScriptedGitHub):
    github.on(
        "GET",
        f"/repos/{REPO}/pulls/comments/101",
        {"id": 101, "pull_request_url": f"{_REPO_API}/pulls/7"},
    )
    github.on("POST", f"/repos/{REPO}/issues/42/comments", _written(1, ISSUE))
    github.on("POST", f"/repos/{REPO}/issues/7/comments", _written(2, PULL))
    github.on("POST", f"/repos/{REPO}/pulls/7/comments/101/replies", _written(3, PULL))
    github.on("POST", f"/repos/{REPO}/pulls/7/reviews", _written(4, PULL))
    github.on("POST", f"/repos/{REPO}/pulls/7/comments", _written(5, PULL))
    github.on("POST", f"/repos/{REPO}/issues", _issue(43))
    github.on("POST", f"/repos/{REPO}/pulls", _pull(8))


_NON_IDEMPOTENT_WRITERS = {
    "ticket_comment": lambda m: member_repository.ticket_comment(m, ISSUE, "Hi"),
    "pr_comment": lambda m: member_repository.pr_comment(m, PULL, "Hi"),
    "pr_reply": lambda m: member_repository.pr_reply(
        m, PULL, "discussion_r101", "Fixed."
    ),
    "pr_review": lambda m: member_repository.pr_review(
        m, PULL, "LGTM", "approve", "abc123"
    ),
    "pr_review_comment": lambda m: member_repository.pr_review_comment(
        m, PULL, "Hmm", "abc123", "a.py", 12, "RIGHT", None, None
    ),
    "ticket_create": lambda m: member_repository.ticket_create(
        m, REPO, "T", "B", False, human_approved=True
    ),
    "pr_create": lambda m: member_repository.pr_create(
        m, REPO, "feature", "main", "T", "B", [ISSUE], "false"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_NON_IDEMPOTENT_WRITERS))
async def test_non_idempotent_writers_never_read_after_the_write(monkeypatch, name):
    # A read that fails after the write landed makes the CLI report failure
    # for a comment that exists, and a retry posts it again. So the target
    # (and everything else) is read before the first write, never after.
    github = _github()
    _writers(github)
    service = _service(github, monkeypatch)
    tickets = ticket_service(github, monkeypatch, service.person)

    result = await _NON_IDEMPOTENT_WRITERS[name](_member(service, tickets))

    methods = [request.method for request in github.requests]
    assert "POST" in methods, name
    assert "GET" not in methods[methods.index("POST") :], (name, github.sent)
    assert result["target"]["url"]


@pytest.mark.asyncio
async def test_pr_comment_reports_the_pull_request_it_commented_on(monkeypatch):
    github = _github().on(
        "POST", f"/repos/{REPO}/issues/7/comments", _written(123, f"{PULL}#c")
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_comment(_member(service), PULL, "Hi")

    assert result == {
        "id": "issuecomment-123",
        "url": f"{PULL}#c",
        "author": LOGIN,
        "created_at": "2026-01-01T00:00:00Z",
        "target": _target().model_dump(),
    }
    assert github.bodies("POST", f"/repos/{REPO}/issues/7/comments") == [{"body": "Hi"}]


@pytest.mark.asyncio
async def test_a_pull_request_comment_needs_a_pull_request(monkeypatch):
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await service.comment(ISSUE, "Hi")

    assert github.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "body", "addressed"),
    [
        ("human", "Run failed.", True),
        (LOGIN, "Run failed.", False),
        ("Human", "@human Run failed.", False),
    ],
)
async def test_a_status_comment_carries_its_marker_and_addresses_the_author(
    monkeypatch, author, body, addressed
):
    """The patrol reads the marker back; the author is told, once, unless the
    member is the author."""
    github = (
        _github()
        .on("GET", f"/repos/{REPO}/pulls/7", _pull(user={"login": author}))
        .on("POST", f"/repos/{REPO}/issues/7/comments", _written(1, PULL))
    )
    service = _service(github, monkeypatch)
    status = WorkflowStatus(reason="failed", person_id="aiko", run_id="r1")

    await service.comment(PULL, body, status=status)
    await service.comment(PULL, body)

    with_status, plain = github.bodies("POST", f"/repos/{REPO}/issues/7/comments")
    assert parse_workflow_status_comment(with_status["body"]) == status
    assert with_status["body"].startswith(f"@{author}\n\n") is addressed
    assert body in with_status["body"]
    assert plain == {"body": body}


# --------------------------------------------------------------------------- #
#   Addresses                                                                 #
# --------------------------------------------------------------------------- #


def test_issue_and_pull_request_urls_name_their_items(monkeypatch):
    service = _service(ScriptedGitHub(), monkeypatch)

    assert service.item(ISSUE, "ticket") == GitHubItem(REPO, 42, "ticket")
    assert service.item(PULL, "pull_request") == GitHubItem(REPO, 7, "pull_request")
    for url, kind in [
        (PULL, "ticket"),
        (ISSUE, "pull_request"),
        ("not-a-url", "ticket"),
        (f"{PULL}/files", "pull_request"),
        (f"{PULL}?diff=split", "pull_request"),
        (f"https://gitlab.com/{REPO}/pull/7", "pull_request"),
        (f"{WEB}/{REPO}/pull/07", "pull_request"),
    ]:
        with pytest.raises(ServiceNotFound):
            service.item(url, kind)


def test_the_remote_names_its_repository_on_the_configured_host(monkeypatch):
    service = _service(ScriptedGitHub(), monkeypatch)

    for remote in (
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
    ):
        assert service.repository_from_remote(remote) == "owner/repo", remote
    for remote in (
        "https://gitlab.com/owner/repo.git",
        "/tmp/remote.git",
        "https://github.com/owner/repo/extra",
        "",
    ):
        assert service.repository_from_remote(remote) is None, remote
    assert service.commit_url("owner/repo", "abc123") == (
        "https://github.com/owner/repo/commit/abc123"
    )


@pytest.mark.asyncio
async def test_a_github_enterprise_server_is_its_own_web_host(monkeypatch):
    person = Person(person_id="aiko", name="Aiko")
    team = Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {
                    "name": "github",
                    "owner": "owner",
                    "api_base_url": "https://ghe.example.test/api/v3",
                },
            },
        ),
        members=[person],
    )
    service = GitHubCodeHostingService(person, team)

    assert service.repository_from_remote("git@ghe.example.test:owner/repo.git") == (
        "owner/repo"
    )
    assert service.repository_from_remote("https://github.com/owner/repo") is None
    assert service.commit_url("owner/repo", "abc123") == (
        "https://ghe.example.test/owner/repo/commit/abc123"
    )
    assert await service.clone_url("owner/repo") == (
        "https://ghe.example.test/owner/repo.git"
    )


@pytest.mark.asyncio
async def test_the_member_pushes_only_to_the_owners_repositories(monkeypatch):
    service = _service(ScriptedGitHub(), monkeypatch)

    service.check_push(REPO)
    service.check_push("ACME/other")
    for repository in ("other/demo", "acme/team/demo", "acme"):
        with pytest.raises(ServiceError, match="'acme'"):
            service.check_push(repository)
    assert await service.clone_url(REPO) == f"{WEB}/{REPO}.git"
    with pytest.raises(ServiceNotFound):
        await service.clone_url("acme/team/demo")


@pytest.mark.asyncio
async def test_a_pull_requests_head_is_where_its_branch_lives(monkeypatch):
    fork = {"sha": "f1", "ref": "topic", "repo": {"full_name": "fork/demo"}}
    gone = {"sha": "f1", "ref": "topic", "repo": None, "label": "fork:topic"}
    own = {"sha": "f1", "ref": "topic", "repo": None, "label": f"{OWNER}:topic"}
    github = ScriptedGitHub().on(
        "GET",
        f"/repos/{REPO}/pulls/7",
        _sequence(_pull(head=fork), _pull(head=own), _pull(head=gone)),
    )
    service = _service(github, monkeypatch)

    assert await service.pull_request_head(PULL) == PullRequestHead(
        repository="fork/demo", branch="topic"
    )
    assert await service.pull_request_head(PULL) == PullRequestHead(
        repository=REPO, branch="topic"
    )
    # A deleted fork leaves no repository to fetch the branch from.
    with pytest.raises(ServiceError, match="not on a repository"):
        await service.pull_request_head(PULL)


@pytest.mark.asyncio
async def test_the_default_branch_is_the_repositorys(monkeypatch):
    github = ScriptedGitHub().on("GET", f"/repos/{REPO}", {"default_branch": "dev"})
    service = _service(github, monkeypatch)

    assert await service.default_branch(REPO) == "dev"
    with pytest.raises(ServiceNotFound):
        await service.default_branch("acme/team/demo")


@pytest.mark.asyncio
async def test_open_pull_requests_asks_for_the_pushed_branch(monkeypatch):
    github = ScriptedGitHub().on(
        "GET", f"/repos/{REPO}/pulls", [{"number": 7, "html_url": PULL}]
    )
    service = _service(github, monkeypatch)

    assert await service.open_pull_requests(REPO, "feature") == [PULL]
    [request] = github.requests
    assert dict(request.url.params) == {"state": "open", "head": "acme:feature"}
    with pytest.raises(ServiceNotFound):
        await service.open_pull_requests("git@github.com:acme/demo.git", "feature")


# --------------------------------------------------------------------------- #
#   The member                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_context_returns_llm_ready_communication_style(monkeypatch):
    person = Person(
        person_id="yuki",
        name="Yuki Nakamura",
        person_type="machine_user",
        speaking_style="柔らかく親しみやすい日本語で話す。",
        roles={
            "programmer": Role(
                id="programmer",
                summary="プロダクトのコードベースを改善する役割。",
                description="",
            )
        },
        profile={
            "character": {
                "archetype": "親しみやすいアイデアメーカー",
                "traits": ["明るい", "共感的"],
                "interests": ["UX"],
                "conversation_preferences": {
                    "contribution_style": ["具体案をやわらかく提案する"]
                },
            }
        },
        account_info={"github_username": "yuki-bot"},
    )
    team = Team(project=Project(name="demo"), members=[person])
    service = GitHubCodeHostingService(person, team)

    result = await member_context(_member(service))

    assert result["speaking_style"] == "柔らかく親しみやすい日本語で話す。"
    style = result["communication_style"]
    assert "Yuki Nakamura" in style["active_member_instruction"]
    assert "親しみやすいアイデアメーカー" in style["voice_basis"]
    assert "具体案をやわらかく提案する" in style["voice_basis"]
    assert "interactive replies" in style["interactive_replies"]
    assert "issue comments" in style["comments"]
    assert "PR titles/bodies" in style["neutral_documents"]
    assert "workflow AgentResponse.message" in style["machine_outputs"]
    assert "token" not in str(style).lower()


@pytest.mark.asyncio
async def test_context_check_credentials_uses_rate_limit_endpoint(monkeypatch):
    # /rate_limit works for every credential type, including GitHub App
    # installation tokens (github_apps) where /user would 403.
    github = ScriptedGitHub().on("GET", "/rate_limit", {"resources": {}})
    service = _service(github, monkeypatch, github_account_type="github_apps")

    result = await member_context(_member(service), check_credentials=True)

    assert result["credential_status"] == "ok"
    assert github.sent == ["GET /rate_limit"]


@pytest.mark.asyncio
async def test_a_refused_credential_fails_the_context_check(monkeypatch):
    github = ScriptedGitHub().on("GET", "/rate_limit", httpx.Response(401))
    service = _service(github, monkeypatch)

    with pytest.raises(
        ServiceError, match=t("integrations.github.response.authentication")
    ):
        await member_context(_member(service), check_credentials=True)


@pytest.mark.asyncio
async def test_context_includes_capability_reference(monkeypatch):
    github = ScriptedGitHub()
    service = _service(github, monkeypatch)

    result = await member_context(_member(service))

    # The context carries the full capability reference (single source of truth)
    # instead of a flat command-name list, and not the old fields.
    assert "guildbotics member chat reply" in result["capabilities"]
    assert "guildbotics member pr create" in result["capabilities"]
    assert "guildbotics member pr review-comment" in result["capabilities"]
    assert "#### Handling review feedback" in result["capabilities"]
    assert "#### レビュー指摘への対応" in result["capabilities"]
    assert "available_member_commands" not in result
    assert "safety_note" not in result
    assert result["credential_status"] == "unchecked"
    assert result["github_username"] == LOGIN
    assert github.requests == []


# --------------------------------------------------------------------------- #
#   Reviews and replies                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reply_answers_the_thread_its_root_comment_starts(monkeypatch):
    github = (
        _github()
        .on(
            "GET",
            f"/repos/{REPO}/pulls/comments/101",
            # Outdated and resolved threads are answered all the same.
            {"id": 101, "pull_request_url": f"{_REPO_API}/pulls/7", "position": None},
        )
        .on(
            "POST",
            f"/repos/{REPO}/pulls/7/comments/101/replies",
            _written(123, f"{PULL}#discussion_r123"),
        )
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_reply(
        _member(service), PULL, "discussion_r101", "Fixed.\n"
    )

    assert result == {
        "id": "discussion_r123",
        "url": f"{PULL}#discussion_r123",
        "created_at": "2026-01-01T00:00:00Z",
        "target": _target().model_dump(),
    }
    assert github.bodies("POST", f"/repos/{REPO}/pulls/7/comments/101/replies") == [
        {"body": "Fixed."}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_to", "root"),
    [
        # A thread of another pull request.
        ("discussion_r101", {"id": 101, "pull_request_url": f"{_REPO_API}/pulls/8"}),
        # A reply, which roots no thread.
        (
            "discussion_r101",
            {
                "id": 101,
                "in_reply_to_id": 99,
                "pull_request_url": f"{_REPO_API}/pulls/7",
            },
        ),
        # No such comment.
        ("discussion_r102", None),
        # Ids of no inline comment.
        ("issuecomment-101", None),
        ("pullrequestreview-101", None),
        ("101", None),
    ],
)
async def test_reply_requires_the_root_comment_of_a_thread_of_this_pr(
    monkeypatch, reply_to, root
):
    github = _github()
    if root is not None:
        github.on("GET", f"/repos/{REPO}/pulls/comments/101", root)
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await member_repository.pr_reply(_member(service), PULL, reply_to, "Fixed")

    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "event"),
    [
        ("approve", "APPROVE"),
        ("request_changes", "REQUEST_CHANGES"),
        ("comment", "COMMENT"),
    ],
)
async def test_pr_review_submits_a_review_on_the_head_the_member_read(
    monkeypatch, verdict, event
):
    github = _github().on(
        "POST",
        f"/repos/{REPO}/pulls/7/reviews",
        _written(555, f"{PULL}#pullrequestreview-555"),
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_review(
        _member(service), PULL, "Looks good.\n", verdict, "abc123"
    )

    assert result == {
        "verdict": verdict,
        "commit": "abc123",
        "url": f"{PULL}#pullrequestreview-555",
        "target": _target().model_dump(),
    }
    assert github.bodies("POST", f"/repos/{REPO}/pulls/7/reviews") == [
        {"body": "Looks good.", "event": event, "commit_id": "abc123"}
    ]


_AT_HEAD = {
    "review": lambda s: s.review(PULL, "LGTM", "approve", "old123"),
    "review_comment": lambda s: s.review_comment(
        PULL, "Hmm", "old123", "a.py", 3, "RIGHT", None, None
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(_AT_HEAD))
async def test_a_review_on_a_head_that_moved_is_refused_unwritten(
    monkeypatch, operation
):
    """A verdict is on the commits the member read: once the head moved, it
    would be on commits nobody reviewed."""
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceError, match="head") as raised:
        await _AT_HEAD[operation](service)

    assert "abc123" in str(raised.value)
    assert github.sent == [f"GET /repos/{REPO}/pulls/7"]


@pytest.mark.asyncio
async def test_pr_review_rejects_unknown_verdict(monkeypatch):
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(MemberCapabilityError, match="Review verdict must be one of"):
        await member_repository.pr_review(_member(service), PULL, "x", "lgtm", "abc")

    assert github.requests == []


@pytest.mark.asyncio
async def test_pr_review_comment_posts_diff_coordinates(monkeypatch):
    github = _github().on(
        "POST",
        f"/repos/{REPO}/pulls/7/comments",
        _written(123, f"{PULL}#discussion_r123"),
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_review_comment(
        _member(service),
        PULL,
        "Please simplify this branch.",
        "abc123",
        "guildbotics/example.py",
        12,
        "RIGHT",
        10,
        "RIGHT",
    )

    assert result == {
        "id": "discussion_r123",
        "url": f"{PULL}#discussion_r123",
        "created_at": "2026-01-01T00:00:00Z",
        "path": "guildbotics/example.py",
        "line": 12,
        "side": "RIGHT",
        "target": _target().model_dump(),
    }
    assert github.bodies("POST", f"/repos/{REPO}/pulls/7/comments") == [
        {
            "body": "Please simplify this branch.",
            "commit_id": "abc123",
            "path": "guildbotics/example.py",
            "line": 12,
            "side": "RIGHT",
            "start_line": 10,
            "start_side": "RIGHT",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_line", "start_side", "message"),
    [
        (10, "LEFT", "start_side must match side"),
        (13, "RIGHT", "start_line must be less than or equal to line"),
        (10, None, "requires both start_line and start_side"),
    ],
)
async def test_pr_review_comment_rejects_invalid_multiline_range(
    monkeypatch, start_line, start_side, message
):
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(MemberCapabilityError, match=message):
        await member_repository.pr_review_comment(
            _member(service),
            PULL,
            "Please simplify this branch.",
            "abc123",
            "guildbotics/example.py",
            12,
            "RIGHT",
            start_line,
            start_side,
        )

    assert github.requests == []


# --------------------------------------------------------------------------- #
#   Reactions                                                                 #
# --------------------------------------------------------------------------- #


def _reacting(github: ScriptedGitHub) -> ScriptedGitHub:
    return github.on_graphql(
        NODE_REPOSITORY,
        {"node": {"repository": {"name": "demo", "owner": {"login": OWNER}}}},
    ).on_graphql(ADD_REACTION, {"addReaction": {"reaction": {"content": "x"}}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_id", "path", "owner"),
    [
        # A pull request's conversation comments are issue comments on GitHub.
        (
            "issuecomment-101",
            "/issues/comments/101",
            ("issue_url", f"{_REPO_API}/issues/7"),
        ),
        (
            "discussion_r101",
            "/pulls/comments/101",
            ("pull_request_url", f"{_REPO_API}/pulls/7"),
        ),
        (
            "pullrequestreview-101",
            "/pulls/7/reviews/101",
            ("pull_request_url", f"{_REPO_API}/pulls/7"),
        ),
        # GitHub answers with the repository's own spelling of its name.
        (
            "discussion_r101",
            "/pulls/comments/101",
            ("pull_request_url", f"{API}/repos/ACME/Demo/pulls/7"),
        ),
    ],
)
async def test_a_reaction_reads_where_its_id_says_then_reacts_to_its_node(
    monkeypatch, target_id, path, owner
):
    github = _reacting(_github()).on(
        "GET",
        f"/repos/{REPO}{path}",
        {"id": 101, "node_id": "NODE_101", owner[0]: owner[1]},
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_reaction(
        _member(service), PULL, target_id, "agree"
    )

    assert result == {"reaction": "agree", "id": target_id}
    assert github.sent == [
        f"GET /repos/{REPO}/pulls/7",
        f"GET /repos/{REPO}{path}",
        "POST /graphql",  # the gate reads where the node lives
        "POST /graphql",
    ]
    assert github.documents()[-1] == {
        "query": ADD_REACTION,
        "variables": {"subject": "NODE_101", "content": "THUMBS_UP"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_id", "path", "found"),
    [
        (
            "issuecomment-101",
            "/issues/comments/101",
            {"issue_url": f"{_REPO_API}/issues/8"},
        ),
        (
            "discussion_r101",
            "/pulls/comments/101",
            {"pull_request_url": f"{_REPO_API}/pulls/8"},
        ),
        (
            "pullrequestreview-101",
            "/pulls/7/reviews/101",
            {"pull_request_url": f"{_REPO_API}/pulls/8"},
        ),
        # A comment named by an id of another kind is read where that kind is.
        (
            "issuecomment-101",
            "/issues/comments/101",
            {"pull_request_url": f"{_REPO_API}/pulls/7"},
        ),
    ],
)
async def test_a_reaction_to_what_is_another_items_is_not_sent(
    monkeypatch, target_id, path, found
):
    github = _reacting(_github()).on(
        "GET", f"/repos/{REPO}{path}", {"id": 101, "node_id": "N", **found}
    )
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound, match="is none of"):
        await member_repository.pr_reaction(_member(service), PULL, target_id, "ack")

    assert github.documents() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_id", ["101", "comment-101", "discussion_r0", "pullrequestreview-x"]
)
async def test_a_reaction_to_an_id_of_no_kind_is_refused_unread(monkeypatch, target_id):
    github = _reacting(_github())
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await service.add_reaction(PULL, target_id, "ack")

    assert github.sent == [f"GET /repos/{REPO}/pulls/7"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reaction", "content"),
    [
        ("ack", "THUMBS_UP"),
        ("agree", "THUMBS_UP"),
        ("celebrate", "HOORAY"),
        ("support", "HEART"),
    ],
)
async def test_each_reaction_is_githubs_own(monkeypatch, reaction, content):
    github = _reacting(_github()).on(
        "GET",
        f"/repos/{REPO}/issues/comments/1",
        {"id": 1, "node_id": "N", "issue_url": f"{_REPO_API}/issues/7"},
    )
    service = _service(github, monkeypatch)

    added = await service.add_reaction(PULL, "issuecomment-1", reaction)

    assert (added.reaction, added.id) == (reaction, "issuecomment-1")
    assert github.documents()[-1]["variables"]["content"] == content


# --------------------------------------------------------------------------- #
#   Readiness                                                                 #
# --------------------------------------------------------------------------- #


def _checks(*runs: dict) -> dict:
    return {"check_runs": list(runs)}


def _run(conclusion: str, name: str = "test", **fields: Any) -> dict:
    return {"name": name, "status": "completed", "conclusion": conclusion, **fields}


def _ci(github: ScriptedGitHub, head: str, checks: dict, base: str = "base123"):
    """The base branch at ``base``, the head ``head`` and its checks."""
    github.on("GET", f"/repos/{REPO}/branches/main", {"commit": {"sha": base}})
    github.on("GET", f"/repos/{REPO}/compare/{base}...{head}", {"behind_by": 0})
    github.on("GET", f"/repos/{REPO}/commits/{head}/check-runs", checks)
    github.on("GET", f"/repos/{REPO}/commits/{head}/status", {"statuses": []})
    return github


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_run", "job_id"),
    [({"id": 9, "run_attempt": 2}, "9/2/90"), ({"id": 9}, "9/1/90")],
)
@pytest.mark.parametrize("mergeable_state", ["clean", "unknown"])
async def test_readiness_reports_rollup_and_tails_failed_job_logs(
    monkeypatch, workflow_run, job_id, mergeable_state
):
    github = _ci(
        _github(),
        "abc123",
        _checks(_run("failure", details_url=f"{WEB}/{REPO}/actions/runs/9")),
    )
    github.on("GET", f"/repos/{REPO}/pulls/7", _pull(mergeable_state=mergeable_state))
    github.on("GET", f"/repos/{REPO}/actions/runs", {"workflow_runs": [workflow_run]})
    github.on(
        "GET",
        f"/repos/{REPO}/actions/runs/9/jobs",
        {
            "jobs": [
                {
                    "id": 90,
                    "name": "Playwright (base)",
                    "conclusion": "failure",
                    "html_url": f"{WEB}/{REPO}/actions/runs/9/job/90",
                },
                {"id": 91, "name": "lint", "conclusion": "success"},
                {"id": 92, "name": "matrix peer", "conclusion": "cancelled"},
            ]
        },
    )
    github.on(
        "GET",
        f"/repos/{REPO}/actions/runs/9/artifacts",
        {
            "artifacts": [
                {"name": "playwright-head-9-2", "expired": False},
                {"name": "old", "expired": True},
            ]
        },
    )
    github.on(
        "GET",
        f"/repos/{REPO}/actions/jobs/90/logs",
        httpx.Response(200, content=b"prefix\nlast line\n"),
    )
    service = _service(github, monkeypatch)

    result = (
        await service.readiness(PULL, failed_logs=True, log_tail_bytes=10)
    ).model_dump()

    assert result["target"] == _target().model_dump()
    assert result["rollup"] == "failure"
    assert result["readiness"] == "blocked"
    assert result["completion_blockers"][0]["code"] == "checks_failed"
    assert result["checks"] == [
        {
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "details_url": f"{WEB}/{REPO}/actions/runs/9",
        }
    ]
    assert result["failed_logs"] == [
        {
            "job_id": job_id,
            "name": "Playwright (base)",
            "conclusion": "failure",
            "url": f"{WEB}/{REPO}/actions/runs/9/job/90",
            "artifact_names": ["playwright-head-9-2"],
            "log": "last line\n",
            "log_bytes": 10,
            "tail_limit_bytes": 10,
            "truncated": True,
        }
    ]
    assert f"GET /repos/{REPO}/actions/jobs/92/logs" not in github.sent


@pytest.mark.asyncio
async def test_readiness_reports_no_checks(monkeypatch):
    github = _ci(_ci(_github(), "abc123", _checks()), "base123", _checks())
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    assert result.rollup == "no_checks"
    assert result.checks == []
    assert result.checks_expected is False
    assert result.readiness == "ready"
    assert result.completion_blockers == []


@pytest.mark.asyncio
async def test_readiness_waits_when_checks_have_not_registered_for_the_head(
    monkeypatch,
):
    github = _ci(_github(), "abc123", _checks())
    github.on(
        "GET", f"/repos/{REPO}/commits/base123/check-runs", _checks(_run("success"))
    )
    github.on("GET", f"/repos/{REPO}/commits/base123/status", {"statuses": []})
    service = _service(github, monkeypatch)

    result = (await service.readiness(PULL)).model_dump()

    assert result["rollup"] == "no_checks"
    assert result["checks_expected"] is True
    assert result["readiness"] == "blocked"
    assert result["completion_blockers"] == [
        {
            "code": "checks_pending",
            "message": "CI checks have not been registered for the PR head yet.",
            "next_action": "Wait for CI to start, then check again.",
        }
    ]


@pytest.mark.asyncio
async def test_readiness_preserves_ci_for_closed_pull_requests(monkeypatch):
    head = {"sha": "merged-head", "ref": "feature", "repo": {"full_name": REPO}}
    github = (
        ScriptedGitHub()
        .on(
            "GET",
            f"/repos/{REPO}/pulls/7",
            _pull(
                state="closed",
                merged_at="2026-09-17T00:00:00Z",
                head=head,
                base={"ref": "deleted-base"},
            ),
        )
        .on(
            "GET",
            f"/repos/{REPO}/commits/merged-head/check-runs",
            _checks(_run("failure")),
        )
        .on("GET", f"/repos/{REPO}/commits/merged-head/status", {"statuses": []})
        .on(
            "GET",
            f"/repos/{REPO}/actions/runs",
            {"workflow_runs": [{"id": 9, "conclusion": "failure"}]},
        )
        .on(
            "GET",
            f"/repos/{REPO}/actions/runs/9/jobs",
            {"jobs": [{"id": 3, "name": "test", "conclusion": "failure"}]},
        )
        .on("GET", f"/repos/{REPO}/actions/runs/9/artifacts", {"artifacts": []})
        .on(
            "GET",
            f"/repos/{REPO}/actions/jobs/3/logs",
            httpx.Response(200, content=b"failure"),
        )
    )
    service = _service(github, monkeypatch)

    result = (await service.readiness(PULL, failed_logs=True)).model_dump()

    assert result["readiness"] == "not_applicable"
    assert result["rollup"] == "failure"
    assert result["checks"][0]["conclusion"] == "failure"
    assert [(log["job_id"], log["log"]) for log in result["failed_logs"]] == [
        ("9/1/3", "failure")
    ]
    assert result["completion_blockers"] == []
    assert not any("/branches/" in p or "/compare/" in p for p in github.sent)


@pytest.mark.asyncio
async def test_readiness_is_ready_after_true_base_integration_and_successful_ci(
    monkeypatch,
):
    github = _ci(_github(), "abc123", _checks(_run("success")))
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    assert result.rollup == "success"
    assert (result.base_sha, result.behind_by, result.out_of_date) == (
        "base123",
        0,
        False,
    )
    assert result.readiness == "ready"
    assert result.completion_blockers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("behind_by", [0, 2])
@pytest.mark.parametrize(
    ("state", "requires_base"),
    [
        ("clean", False),
        ("unstable", False),
        ("has_hooks", False),
        ("behind", True),
        ("blocked", True),
        ("dirty", True),
        ("draft", True),
        ("unknown", True),
        ("new-state", True),
        ("", True),
        (None, True),
        ("missing", True),
    ],
)
async def test_readiness_ignores_lag_only_when_the_host_explicitly_allows_merging(
    monkeypatch, state, requires_base, behind_by
):
    github = _ci(_github(), "abc123", _checks(_run("success")))
    pull = _pull(mergeable_state=state)
    if state == "missing":
        del pull["mergeable_state"]
    github.on("GET", f"/repos/{REPO}/pulls/7", pull)
    github.on(
        "GET", f"/repos/{REPO}/compare/base123...abc123", {"behind_by": behind_by}
    )
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    blocked = requires_base and behind_by > 0
    assert (result.behind_by, result.out_of_date) == (behind_by, behind_by > 0)
    assert result.readiness == ("blocked" if blocked else "ready")
    assert [b.code for b in result.completion_blockers] == (
        ["base_out_of_date"] if blocked else []
    )


@pytest.mark.asyncio
async def test_readiness_can_be_retried_after_the_host_allows_merging(monkeypatch):
    github = _ci(_github(), "abc123", _checks(_run("success")))
    github.on("GET", f"/repos/{REPO}/compare/base123...abc123", {"behind_by": 2})
    github.on(
        "GET",
        f"/repos/{REPO}/pulls/7",
        _sequence(
            _pull(mergeable_state="unknown"), _pull(mergeable_state="unknown"), _pull()
        ),
    )
    service = _service(github, monkeypatch)

    blocked = await service.readiness(PULL)
    assert [item.code for item in blocked.completion_blockers] == ["base_out_of_date"]
    ready = await service.readiness(PULL)
    assert ready.readiness == "ready"
    assert (ready.behind_by, ready.out_of_date) == (2, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "current", "blocked"),
    [("clean", "blocked", True), ("blocked", "clean", False)],
)
async def test_readiness_uses_the_final_host_merge_state(
    monkeypatch, initial, current, blocked
):
    github = _ci(_github(), "abc123", _checks(_run("success")))
    github.on("GET", f"/repos/{REPO}/compare/base123...abc123", {"behind_by": 2})
    github.on(
        "GET",
        f"/repos/{REPO}/pulls/7",
        _sequence(_pull(mergeable_state=initial), _pull(mergeable_state=current)),
    )
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    assert result.readiness == ("blocked" if blocked else "ready")
    assert [item.code for item in result.completion_blockers] == (
        ["base_out_of_date"] if blocked else []
    )


@pytest.mark.asyncio
async def test_readiness_blocks_a_head_that_changes_during_the_check(monkeypatch):
    old = {"sha": "old-head", "ref": "feature", "repo": {"full_name": REPO}}
    new = {"sha": "new-head", "ref": "feature", "repo": {"full_name": REPO}}
    github = _ci(ScriptedGitHub(), "old-head", _checks(_run("success")))
    github.on(
        "GET", f"/repos/{REPO}/pulls/7", _sequence(_pull(head=old), _pull(head=new))
    )
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    assert (result.head_sha, result.current_head_sha) == ("old-head", "new-head")
    assert result.readiness == "blocked"
    assert [item.code for item in result.completion_blockers] == ["head_changed"]


@pytest.mark.asyncio
async def test_readiness_blocks_a_base_that_changes_during_the_check(monkeypatch):
    github = _ci(_github(), "abc123", _checks(_run("success")), base="base-one")
    github.on(
        "GET",
        f"/repos/{REPO}/branches/main",
        _sequence({"commit": {"sha": "base-one"}}, {"commit": {"sha": "base-two"}}),
    )
    service = _service(github, monkeypatch)

    result = await service.readiness(PULL)

    assert (result.base_sha, result.current_base_sha) == ("base-one", "base-two")
    assert result.readiness == "blocked"
    assert [item.code for item in result.completion_blockers] == ["base_changed"]


@pytest.mark.asyncio
async def test_readiness_reports_the_permission_ci_needs(monkeypatch):
    github = _ci(_github(), "abc123", httpx.Response(403, text="SECRET"))
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceError) as raised:
        await service.readiness(PULL)

    assert str(raised.value) == " ".join(
        [
            t("integrations.github.response.forbidden"),
            t(
                "integrations.github.response.permission",
                permission="Actions / Checks / Commit statuses",
            ),
        ]
    )


# --------------------------------------------------------------------------- #
#   Artifacts                                                                 #
# --------------------------------------------------------------------------- #


def _artifact_zip(files: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return output.getvalue()


def _artifacts(content: bytes, *artifacts: dict) -> ScriptedGitHub:
    """Pull request #7, whose head ``abc123`` was built into ``artifacts``;
    artifact 12's archive is ``content``."""
    return (
        _github()
        .on(
            "GET",
            f"/repos/{REPO}/actions/artifacts",
            {
                "artifacts": [
                    {"workflow_run": {"id": 9, "head_sha": "abc123"}, **artifact}
                    for artifact in artifacts
                ]
            },
        )
        .on(
            "GET",
            f"/repos/{REPO}/actions/artifacts/12/zip",
            httpx.Response(200, content=content),
        )
    )


def _report(name: str = "report", **fields: Any) -> dict:
    return {"id": 12, "name": name, "expired": False, "size_in_bytes": 100, **fields}


@pytest.mark.asyncio
async def test_artifact_download_extracts_the_named_artifact_of_the_head(
    monkeypatch, tmp_path
):
    github = _artifacts(
        _artifact_zip({"error-context.md": b"failure details"}),
        _report("playwright-report"),
        # Of another head, newer, and expired: none of them is the head's.
        {**_report("playwright-report", id=13), "workflow_run": {"head_sha": "old"}},
        _report("playwright-report", id=14, expired=True),
    )
    service = _service(github, monkeypatch)

    result = await member_repository.artifact_download(
        _member(service), PULL, "playwright-report", tmp_path
    )

    assert result == {
        "name": "playwright-report",
        "id": "12",
        "destination": str(tmp_path.resolve()),
        "files": [str((tmp_path / "error-context.md").resolve())],
    }
    assert (tmp_path / "error-context.md").read_bytes() == b"failure details"
    [listing] = [r for r in github.requests if r.url.path.endswith("/artifacts")]
    assert listing.url.params["name"] == "playwright-report"


@pytest.mark.asyncio
async def test_a_head_without_the_artifact_has_none(monkeypatch, tmp_path):
    github = _artifacts(b"", _report("other"))
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound, match="no artifact 'report'"):
        await member_repository.artifact_download(
            _member(service), PULL, "report", tmp_path
        )


@pytest.mark.asyncio
async def test_artifact_download_rejects_oversized_artifact(monkeypatch, tmp_path):
    github = _artifacts(b"", _report(size_in_bytes=MAX_ARTIFACT_BYTES + 1))
    service = _service(github, monkeypatch)
    # The destination is a directory of its own: ``tmp_path`` also holds the
    # isolated HOME, which anything in the process may write to.
    destination = tmp_path / "artifact"

    with pytest.raises(ServiceError, match="Ask a human to retrieve it"):
        await member_repository.artifact_download(
            _member(service), PULL, "report", destination
        )

    assert list(destination.rglob("*")) == []
    assert not any(p.endswith("/zip") for p in github.sent)


@pytest.mark.asyncio
async def test_artifact_download_rejects_path_traversal(monkeypatch, tmp_path):
    github = _artifacts(_artifact_zip({"../outside.txt": b"escape"}), _report())
    service = _service(github, monkeypatch)

    with pytest.raises(MemberCapabilityError, match="unsafe path"):
        await member_repository.artifact_download(
            _member(service), PULL, "report", tmp_path / "work"
        )

    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.asyncio
async def test_a_command_of_an_environment_has_its_artifact_unpacked_there(
    monkeypatch, tmp_path, symlinks
):
    """The host downloads it and writes none of it where the environment
    can: a destination the environment made a link to elsewhere leads the
    host nowhere, since only the environment unpacks it."""
    archive = _artifact_zip({"report.md": b"details"})
    unpacked = {"destination": "/guest/work", "files": ["/guest/work/report.md"]}
    guest = GuestDouble(GuestResult(0, json.dumps(unpacked).encode(), b""))
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "work").mkdir()
    destination = tmp_path / "work" / "artifact"
    destination.symlink_to(outside, target_is_directory=True)
    service = _service(_artifacts(archive, _report()), monkeypatch)

    with member_invocation_scope(MemberInvocation(guest=guest)):
        result = await member_repository.artifact_download(
            _member(service), PULL, "report", destination
        )

    [run] = guest.runs
    # Nothing of the host's: not where it works, nor its variables.
    assert (run["cwd"], run["env"], run["stdout_limit"]) == ("/", {}, STREAM_READ_LIMIT)
    assert run["argv"] == [
        "python",
        "-m",
        "guildbotics.capabilities.artifact_archive",
        f"/guest{destination}",
    ]
    assert run["read"] == archive
    # Held where no environment ever writes: the host's own temporary root.
    assert Path(str(run["stdin"])).is_relative_to(host_temporary_root())
    assert not Path(str(run["stdin"])).exists()
    assert list(outside.iterdir()) == []
    assert result == {"name": "report", "id": "12", **unpacked}


@pytest.mark.asyncio
async def test_an_artifact_the_environment_refused_fails_the_command(
    monkeypatch, tmp_path
):
    guest = GuestDouble(
        GuestResult(1, b"", b"Artifact contains an unsafe path: ../x\n")
    )
    service = _service(_artifacts(_artifact_zip({"../x": b""}), _report()), monkeypatch)

    with (
        member_invocation_scope(MemberInvocation(guest=guest)),
        pytest.raises(MemberCapabilityError, match="unsafe path: ../x"),
    ):
        await member_repository.artifact_download(
            _member(service), PULL, "report", tmp_path
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (GuestProcessError("The member command ran out of time."), "ran out of time"),
        (GuestResult(0, b"[]", b""), "cannot have been"),
        (GuestResult(0, b'{"destination": 1, "files": []}', b""), "cannot have been"),
        (GuestResult(0, b"not json", b""), "cannot have been"),
    ],
)
async def test_what_the_environment_answers_is_read_as_no_more_than_it_can_be(
    monkeypatch, tmp_path, answer, message
):
    """It writes its answer where the agent's code runs: one that is not
    where it unpacked fails the command, and says nothing of the host."""
    guest = GuestDouble(answer)
    service = _service(_artifacts(_artifact_zip({"a": b""}), _report()), monkeypatch)

    with (
        member_invocation_scope(MemberInvocation(guest=guest)),
        pytest.raises(MemberCapabilityError, match=message),
    ):
        await member_repository.artifact_download(
            _member(service), PULL, "report", tmp_path
        )


@pytest.mark.asyncio
async def test_what_the_environment_answers_decides_nothing_else(monkeypatch, tmp_path):
    """Fields the host decides stay the host's, whatever the answer adds."""
    answer = {"destination": "/w", "files": [], "name": "else", "id": "1"}
    guest = GuestDouble(GuestResult(0, json.dumps(answer).encode(), b""))
    service = _service(_artifacts(_artifact_zip({"a": b""}), _report()), monkeypatch)

    with member_invocation_scope(MemberInvocation(guest=guest)):
        result = await member_repository.artifact_download(
            _member(service), PULL, "report", tmp_path
        )

    assert (result["name"], result["id"]) == ("report", "12")


# --------------------------------------------------------------------------- #
#   Tickets through the member commands                                       #
# --------------------------------------------------------------------------- #


def _tickets(github: ScriptedGitHub, monkeypatch):
    service = _service(github, monkeypatch)
    tickets = ticket_service(github, monkeypatch, service.person)
    tickets._project_node_id = "PROJECT"
    return _member(service, tickets)


@pytest.mark.asyncio
async def test_ticket_create_opens_the_ticket_and_puts_it_on_the_board(monkeypatch):
    github = (
        _github()
        .on("POST", f"/repos/{REPO}/issues", _issue(43, title="Follow-up"))
        .on_graphql(
            NODE_REPOSITORY,
            {"node": {"repository": {"name": "demo", "owner": {"login": OWNER}}}},
        )
        .on_graphql(ADD_PROJECT_ITEM, {"addProjectV2ItemById": {"item": {"id": "X"}}})
    )

    result = await member_repository.ticket_create(
        _tickets(github, monkeypatch),
        REPO,
        "Follow-up",
        "Body",
        True,
        human_approved=True,
    )

    assert result == {
        "labels": [],
        "target": _target(issue_url(43), "Follow-up").model_dump(),
    }
    assert github.bodies("POST", f"/repos/{REPO}/issues") == [
        {"title": "Follow-up", "body": "Body"}
    ]
    assert github.documents()[-1] == {
        "query": ADD_PROJECT_ITEM,
        "variables": {"proj": "PROJECT", "content": "I_43"},
    }


@pytest.mark.asyncio
async def test_a_failed_board_add_is_reported_after_the_ticket_was_created(
    monkeypatch,
):
    """The ticket exists; the member is told the board add failed, in one line
    the member CLI shows, never GitHub's raw errors."""
    github = (
        _github()
        .on("POST", f"/repos/{REPO}/issues", _issue(43))
        .on_graphql(
            NODE_REPOSITORY,
            {"node": {"repository": {"name": "demo", "owner": {"login": OWNER}}}},
        )
        .on_graphql(
            ADD_PROJECT_ITEM,
            httpx.Response(200, json={"errors": [{"message": "SECRET project"}]}),
        )
    )

    with pytest.raises(ServiceError) as raised:
        await member_repository.ticket_create(
            _tickets(github, monkeypatch), REPO, "T", "B", True, human_approved=True
        )

    assert str(raised.value).startswith(
        t(
            "integrations.github.response.created_off_board",
            url=issue_url(43),
            message="",
        )
    )
    assert "SECRET" not in str(raised.value)
    assert github.writes[0] == f"POST /repos/{REPO}/issues"


@pytest.mark.asyncio
async def test_ticket_create_without_human_approval_is_refused_before_any_call(
    monkeypatch,
):
    github = _github()

    with pytest.raises(MemberCapabilityError, match="requires a human instruction"):
        await member_repository.ticket_create(
            _tickets(github, monkeypatch), REPO, "Follow-up", "Body", False
        )

    assert github.requests == []


@pytest.mark.asyncio
async def test_ticket_create_rejects_a_label_the_tracker_does_not_define(monkeypatch):
    github = _github().on("GET", f"/repos/{REPO}/labels", pages([{"name": "cli"}]))

    with pytest.raises(ServiceError) as error:
        await member_repository.ticket_create(
            _tickets(github, monkeypatch),
            REPO,
            "Follow-up",
            "Body",
            False,
            labels=["priority: high"],
            human_approved=True,
        )

    message = str(error.value)
    assert f"'priority: high' is not defined in {REPO}" in message
    assert "Defined labels: cli" in message
    assert github.writes == []


@pytest.mark.asyncio
async def test_ticket_comment_reports_the_ticket_it_commented_on(monkeypatch):
    github = _github().on(
        "POST",
        f"/repos/{REPO}/issues/42/comments",
        _written(123, f"{ISSUE}#issuecomment-123"),
    )

    result = await member_repository.ticket_comment(
        _tickets(github, monkeypatch), ISSUE, "Comment"
    )

    assert result == {
        "id": "issuecomment-123",
        "url": f"{ISSUE}#issuecomment-123",
        "author": LOGIN,
        "created_at": "2026-01-01T00:00:00Z",
        "target": _target(ISSUE, "issue 42").model_dump(),
    }


@pytest.mark.asyncio
async def test_ticket_update_patches_only_the_body(monkeypatch):
    github = _github().on(
        "PATCH",
        f"/repos/{REPO}/issues/42",
        _issue(body=None, labels=[{"name": "cli"}]),
    )

    result = await member_repository.ticket_update(
        _tickets(github, monkeypatch), ISSUE, "Updated body"
    )

    assert result == {
        "body": "",
        "state": "open",
        "state_changed": False,
        "labels": ["cli"],
        "target": _target(ISSUE, "issue 42").model_dump(),
    }
    assert github.bodies("PATCH", f"/repos/{REPO}/issues/42") == [
        {"body": "Updated body"}
    ]
    # Nothing is validated against the tracker's labels when none is added.
    assert f"GET /repos/{REPO}/labels" not in github.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "changed"), [("open", True), ("closed", False)])
async def test_ticket_update_closes_the_ticket_with_its_state_reason(
    monkeypatch, before, changed
):
    github = (
        _github()
        .on("GET", f"/repos/{REPO}/issues/42", _issue(state=before))
        .on("PATCH", f"/repos/{REPO}/issues/42", _issue(state="closed"))
    )

    result = await member_repository.ticket_update(
        _tickets(github, monkeypatch),
        ISSUE,
        state="closed",
        state_reason="not_planned",
        human_approved=True,
    )

    assert github.bodies("PATCH", f"/repos/{REPO}/issues/42") == [
        {"state": "closed", "state_reason": "not_planned"}
    ]
    assert (result["state"], result["state_changed"]) == ("closed", changed)


@pytest.mark.parametrize("state", ["closed", "open"])
@pytest.mark.asyncio
async def test_ticket_update_state_change_without_human_approval_is_refused(
    monkeypatch, state
):
    github = _github()

    with pytest.raises(MemberCapabilityError) as error:
        await member_repository.ticket_update(
            _tickets(github, monkeypatch), ISSUE, state=state
        )

    assert f"Changing the ticket state to '{state}'" in str(error.value)
    assert github.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [{}, {"remove_labels": ["   "]}, {"add_labels": ["", " "]}],
)
async def test_ticket_update_without_any_requested_change_is_refused(
    monkeypatch, change
):
    github = _github()

    with pytest.raises(MemberCapabilityError, match="at least one of body, title"):
        await member_repository.ticket_update(
            _tickets(github, monkeypatch), ISSUE, **change
        )

    assert github.requests == []


@pytest.mark.asyncio
async def test_ticket_update_patches_fields_before_labels_and_rereads_the_result(
    monkeypatch,
):
    github = (
        _github()
        .on("GET", f"/repos/{REPO}/labels", pages([{"name": "cli"}]))
        .on(
            "GET",
            f"/repos/{REPO}/issues/42",
            _sequence(_issue(), _issue(title="New", labels=[{"name": "cli"}])),
        )
        .on("PATCH", f"/repos/{REPO}/issues/42", _issue(title="New"))
        .on("POST", f"/repos/{REPO}/issues/42/labels", [])
        .on("DELETE", f"/repos/{REPO}/issues/42/labels/docs", [])
    )

    result = await member_repository.ticket_update(
        _tickets(github, monkeypatch),
        ISSUE,
        title="New",
        add_labels=["  CLI "],
        remove_labels=["docs", ""],
    )

    assert github.sent == [
        f"GET /repos/{REPO}/labels",
        f"GET /repos/{REPO}/issues/42",
        f"PATCH /repos/{REPO}/issues/42",
        f"DELETE /repos/{REPO}/issues/42/labels/docs",
        f"POST /repos/{REPO}/issues/42/labels",
        f"GET /repos/{REPO}/issues/42",
    ]
    assert github.bodies("POST", f"/repos/{REPO}/issues/42/labels") == [
        {"labels": ["cli"]}
    ]
    assert result["labels"] == ["cli"]


@pytest.mark.asyncio
async def test_ticket_update_leaves_labels_untouched_when_the_field_patch_fails(
    monkeypatch,
):
    github = (
        _github()
        .on("GET", f"/repos/{REPO}/labels", pages([{"name": "cli"}]))
        .on(
            "PATCH",
            f"/repos/{REPO}/issues/42",
            httpx.Response(422, json={"message": "SECRET"}),
        )
    )

    with pytest.raises(ServiceError) as raised:
        await member_repository.ticket_update(
            _tickets(github, monkeypatch),
            ISSUE,
            body="Body",
            add_labels=["cli"],
            remove_labels=["docs"],
        )

    assert str(raised.value) == t("integrations.github.response.rejected")
    assert github.writes == [f"PATCH /repos/{REPO}/issues/42"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["not-a-url", PULL])
async def test_ticket_update_refuses_what_is_no_issue_url(monkeypatch, url):
    github = _github()

    with pytest.raises(ServiceNotFound):
        await member_repository.ticket_update(_tickets(github, monkeypatch), url, "B")

    assert github.requests == []


# --------------------------------------------------------------------------- #
#   Opening and updating pull requests                                        #
# --------------------------------------------------------------------------- #


async def _create(service, base="main", ticket_urls=(), draft="false", closes=False):
    return await member_repository.pr_create(
        _member(service),
        REPO,
        "feature",
        base,
        "Title",
        "Body",
        ticket_urls,
        draft,
        closes,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [False, True])
async def test_pr_create_opens_one_on_the_base_it_is_given(monkeypatch, draft):
    github = _github().on("POST", f"/repos/{REPO}/pulls", _pull(8, draft=draft))
    service = _service(github, monkeypatch)

    result = await _create(service, "ticket-driven-workflow", draft=str(draft).lower())

    assert result == {
        "created": True,
        "draft": draft,
        "head": "feature",
        "base": "ticket-driven-workflow",
        "target": _target(pull_url(8), "pr 8").model_dump(),
    }
    [lookup] = [r for r in github.requests if r.method == "GET"]
    assert dict(lookup.url.params) == {
        "head": "acme:feature",
        "base": "ticket-driven-workflow",
        "state": "open",
    }
    assert github.bodies("POST", f"/repos/{REPO}/pulls") == [
        {
            "title": "Title",
            "head": "feature",
            "base": "ticket-driven-workflow",
            "body": "Body",
            "draft": draft,
        }
    ]


@pytest.mark.asyncio
async def test_pr_create_uses_the_default_branch_when_base_is_empty(monkeypatch):
    github = (
        _github()
        .on("GET", f"/repos/{REPO}", {"default_branch": "develop"})
        .on("POST", f"/repos/{REPO}/pulls", _pull(9))
    )
    service = _service(github, monkeypatch)

    result = await _create(service, "")

    assert result["base"] == "develop"
    assert github.sent[:2] == [f"GET /repos/{REPO}", f"GET /repos/{REPO}/pulls"]
    assert github.bodies("POST", f"/repos/{REPO}/pulls")[0]["base"] == "develop"


@pytest.mark.asyncio
async def test_pr_create_returns_the_open_pr_of_the_head_without_updating_it(
    monkeypatch,
):
    github = _github().on("GET", f"/repos/{REPO}/pulls", [_pull(7, draft=False)])
    service = _service(github, monkeypatch)

    result = await _create(service, ticket_urls=[ISSUE], draft="true")

    assert result == {
        "created": False,
        "draft": False,
        "head": "feature",
        "base": "main",
        "target": _target().model_dump(),
    }
    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticket_urls", "closes", "body"),
    [
        ([ISSUE], False, "Body\n\nRefs #42"),
        ([ISSUE], True, "Body\n\nCloses #42"),
        # Each ticket gets its own line.
        ([ISSUE, issue_url(43)], True, "Body\n\nCloses #42\n\nCloses #43"),
        # Another repository's issue, or another service's ticket, is
        # referred to by its URL.
        (
            [ISSUE, issue_url(5, "acme/other")],
            False,
            f"Body\n\nRefs #42\n\nRefs {issue_url(5, 'acme/other')}",
        ),
        (
            ["https://tickets.example.com/browse/OPS-7"],
            False,
            "Body\n\nRefs https://tickets.example.com/browse/OPS-7",
        ),
    ],
)
async def test_pr_create_links_the_tickets(monkeypatch, ticket_urls, closes, body):
    github = (
        _github()
        .on("GET", f"/repos/{REPO}/issues/43", _issue(43))
        .on("GET", "/repos/acme/other/issues/5", _issue(5))
        .on("POST", f"/repos/{REPO}/pulls", _pull(8))
    )
    service = _service(github, monkeypatch)

    await _create(service, ticket_urls=ticket_urls, closes=closes)

    assert github.bodies("POST", f"/repos/{REPO}/pulls")[0]["body"] == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticket_url", "error"),
    [
        # A merge closes only an issue of its own repository.
        (issue_url(5, "acme/other"), ServiceUnsupported),
        ("https://tickets.example.com/browse/OPS-7", ServiceUnsupported),
        # No such issue, and a pull request's number.
        (issue_url(404), ServiceNotFound),
        (issue_url(8), ServiceNotFound),
    ],
)
async def test_pr_create_refuses_a_ticket_it_cannot_close_before_opening(
    monkeypatch, ticket_url, error
):
    github = (
        _github()
        .on("GET", "/repos/acme/other/issues/5", _issue(5))
        .on("GET", f"/repos/{REPO}/issues/8", _issue(8, pull_request={}))
    )
    service = _service(github, monkeypatch)

    with pytest.raises(error):
        # The repository's own issue first: one ticket it cannot close is
        # enough to open nothing.
        await _create(service, ticket_urls=[ISSUE, ticket_url], closes=True)

    assert github.writes == []


@pytest.mark.asyncio
async def test_pr_create_outside_the_owner_is_refused_before_it_is_sent(monkeypatch):
    github = ScriptedGitHub().on("GET", "/repos/other/demo/pulls", [])
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceError, match="'acme'"):
        await member_repository.pr_create(
            _member(service), "other/demo", "feature", "main", "T", "B", (), "false"
        )

    assert github.writes == []


@pytest.mark.asyncio
async def test_pr_update_replaces_the_title_without_touching_the_body(monkeypatch):
    github = _github().on(
        "PATCH", f"/repos/{REPO}/pulls/7", _pull(title="New title", body="Untouched")
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_update(
        _member(service), PULL, title="New title"
    )

    assert github.sent == [f"PATCH /repos/{REPO}/pulls/7"]
    assert github.bodies("PATCH", f"/repos/{REPO}/pulls/7") == [{"title": "New title"}]
    assert result == {
        "body": "Untouched",
        "target": _target(PULL, "New title").model_dump(),
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Updated body", "Updated body\n\nCloses #42\n\nRefs #43"),
        ("", "Closes #42\n\nRefs #43"),
        ("Refs #42", "Closes #42\n\nRefs #43"),
        ("Closes #43", "Closes #42\n\nRefs #43"),
    ],
)
@pytest.mark.asyncio
async def test_pr_update_preserves_existing_issue_links(monkeypatch, body, expected):
    github = (
        _github()
        .on(
            "GET",
            f"/repos/{REPO}/pulls/7",
            _pull(body="Old body\n\nCloses #42\nRefs #43"),
        )
        .on("PATCH", f"/repos/{REPO}/pulls/7", lambda r: _pull(**json.loads(r.content)))
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_update(
        _member(service), PULL, body, title="New title"
    )

    assert result["body"] == expected
    assert github.bodies("PATCH", f"/repos/{REPO}/pulls/7") == [
        {"body": expected, "title": "New title"}
    ]
    assert github.sent == [f"GET /repos/{REPO}/pulls/7", f"PATCH /repos/{REPO}/pulls/7"]


@pytest.mark.parametrize("body", ["", "New\n\nRefs #43"])
@pytest.mark.asyncio
async def test_pr_update_empty_ticket_links_removes_body_links(monkeypatch, body):
    github = _github().on(
        "PATCH", f"/repos/{REPO}/pulls/7", lambda r: _pull(**json.loads(r.content))
    )
    service = _service(github, monkeypatch)

    result = await member_repository.pr_update(_member(service), PULL, body, tickets=[])

    assert result["body"] == body.split("\n\nRefs")[0]
    assert github.sent == [f"PATCH /repos/{REPO}/pulls/7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"body": "New"}, {"tickets": []}])
async def test_pr_update_refuses_to_patch_when_the_existing_body_cannot_be_read(
    monkeypatch,
    change,
):
    github = ScriptedGitHub().on(
        "GET", f"/repos/{REPO}/pulls/7", httpx.Response(502, text="down")
    )
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceError, match="HTTP 502"):
        await member_repository.pr_update(_member(service), PULL, **change)

    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({}, "pr update needs a body, a title, or ticket links."),
    ],
)
async def test_pr_update_needs_what_it_changes(monkeypatch, change, message):
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(MemberCapabilityError, match=message):
        await member_repository.pr_update(_member(service), PULL, **change)

    assert github.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["not-a-url", issue_url(7)])
async def test_pr_update_refuses_what_is_no_pull_request_url(monkeypatch, url):
    github = _github()
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await member_repository.pr_update(_member(service), url, "Body")

    assert github.requests == []


@pytest.mark.asyncio
async def test_pr_update_reports_what_github_refused(monkeypatch):
    github = _github().on(
        "PATCH", f"/repos/{REPO}/pulls/7", httpx.Response(400, text="SECRET")
    )
    service = _service(github, monkeypatch)

    with pytest.raises(ServiceError) as raised:
        await member_repository.pr_update(_member(service), PULL, "Body", tickets=[])

    assert str(raised.value) == t("integrations.github.response.http", status=400)


# --------------------------------------------------------------------------- #
#   Issue links of a pull request body                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "closes", "expected"),
    [
        ("Body", False, "Body\n\nRefs #42"),
        ("Body", True, "Body\n\nCloses #42"),
        ("", True, "Closes #42"),
        ("Closes #42", True, "Closes #42"),
        ("Fixes #42", True, "Fixes #42"),
        ("Resolved #42", True, "Resolved #42"),
        ("Refs #42", False, "Refs #42"),
        ("Refs #42", True, "Closes #42"),
        ("Ref #42", True, "Closes #42"),
        ("Body\n\nRefs #42", True, "Body\n\nCloses #42"),
        ("Refs #42\n\nCloses #42", True, "Refs #42\n\nCloses #42"),
        ("Closes #42", False, "Closes #42"),
        ("See #42", False, "See #42\n\nRefs #42"),
        ("Closes #41", True, "Closes #41\n\nCloses #42"),
    ],
)
def test_append_issue_link_chooses_trailer_and_skips_existing_keywords(
    body, closes, expected
):
    assert append_issue_link(body, 42, closes=closes) == expected


@pytest.mark.parametrize(
    "keyword",
    [
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
        "ref",
        "refs",
    ],
)
def test_preserve_issue_links_keeps_keyword_variants(keyword):
    trailer = f"{keyword.upper()} #42"
    assert preserve_issue_links("New", f"Old\n{trailer}") == f"New\n\n{trailer}"


@pytest.mark.parametrize(
    ("body", "previous", "expected"),
    [
        ("Refs #42", "Closes #42", "Closes #42"),
        ("Fixes #42", "Refs #42", "Refs #42"),
        ("Closes #42", "Fixes #42", "Fixes #42"),
        ("", "Old\nCloses #42\nRefs #43", "Closes #42\n\nRefs #43"),
        ("New", "Refs #42\nRefs #42", "New\n\nRefs #42"),
        ("New", "Refs #42\nCloses #42", "New\n\nRefs #42\n\nCloses #42"),
        ("New\n", "Old without links", "New\n"),
        ("New", "See #42; prefixedRefs #43; Closes #44suffix", "New"),
        ("Refs #420", "Closes #42", "Closes #42"),
        ("See #42", "Closes #42", "See #42\n\nCloses #42"),
        ("Refs #43", "Closes #42\nRefs #43", "Closes #42\n\nRefs #43"),
        ("Use `tool`.\nRefs #42", "Closes #42", "Use `tool`.\n\nCloses #42"),
        ("New", "Use `tool`.\nCloses #42", "New\n\nCloses #42"),
        ("New", "Text\r\rCloses #99\n```\nCloses #42\n```", "New\n\nCloses #99"),
        ("New", "Text\r\rCloses #42\rRefs #43", "New\n\nCloses #42\n\nRefs #43"),
        ("New", "  Closes #42  \r\nRefs\t#43", "New\n\nCloses #42\n\nRefs\t#43"),
    ],
)
def test_preserve_issue_links_keeps_previous_links_and_deduplicates(
    body, previous, expected
):
    assert preserve_issue_links(body, previous) == expected


@pytest.mark.parametrize(
    "example",
    [
        "Example: `Closes #617`",
        "Do not use Closes #617 here.",
        "`Closes #617`",
        "```text\nCloses #617\n```",
        "~~~\nCloses #617\n~~~",
        "    Closes #617",
        "> Closes #617",
        "> example\nCloses #617",
        "<!--\nCloses #617\n-->",
        "<pre>\nCloses #617\n</pre>",
        "Closes &#35;617",
        "Closes \\#617",
        "Closes\n#617",
        "`example\nCloses #617\nexample`",
    ],
)
def test_issue_link_examples_do_not_become_trailers_or_override_real_links(example):
    assert (
        preserve_issue_links("New", f"{example}\n\nCloses #630") == "New\n\nCloses #630"
    )
    assert preserve_issue_links(example, "Closes #617") == f"{example}\n\nCloses #617"
    assert append_issue_link(example, 617, closes=True) == f"{example}\n\nCloses #617"


def test_create_upgrades_only_the_real_reference_link():
    body = "Example: `Refs #42`\n\nRefs #42"
    assert append_issue_link(body, 42, closes=True) == (
        "Example: `Refs #42`\n\nCloses #42"
    )


def test_nested_image_code_does_not_mask_an_adjacent_link():
    body = "Refs #42\n![`x`](https://example.com)"
    assert (
        preserve_issue_links(body, "Closes #42")
        == "![`x`](https://example.com)\n\nCloses #42"
    )
    assert preserve_issue_links("New", body) == "New\n\nRefs #42"


@pytest.mark.parametrize("body", ["```text\nNew", "~~~\nNew", "<!-- New", "<pre>\nNew"])
def test_appending_issue_links_refuses_unclosed_markdown_blocks(body):
    with pytest.raises(ServiceError, match="Close the open code or HTML"):
        preserve_issue_links(body, "Closes #42")
    with pytest.raises(ServiceError, match="Close the open code or HTML"):
        append_issue_link(body, 42, closes=True)
    with pytest.raises(ServiceError, match="Close the open code or HTML"):
        append_external_issue_link(body, "https://tickets.example.com/T-1")


@pytest.mark.parametrize("target", ["#42", "https://tickets.example.com/T-1"])
@pytest.mark.parametrize(
    "example",
    [
        "`Refs {target}`",
        "> Refs {target}",
        "```\nRefs {target}\n```",
        "<!--\nRefs {target}\n-->",
        "<pre>\nRefs {target}\n</pre>",
    ],
)
def test_replacing_links_retains_examples_and_removes_real_link_lines(target, example):
    example = example.format(target=target)
    body = f"Context\n\n{example}\n\n- Refs {target}\n\nCloses #43"
    assert remove_issue_links(body) == f"Context\n\n{example}"
    assert (
        preserve_issue_links("New", f"{example}\n\nRefs {target}")
        == f"New\n\nRefs {target}"
    )


@pytest.mark.parametrize(
    "prefix", ["- ", "+ ", "* ", "1. ", "1) ", "- - ", "- item\n  "]
)
def test_list_links_are_preserved_and_only_explicit_updates_change_links(prefix):
    body = f"{prefix}Refs #42\r\n"
    assert preserve_issue_links("New", body) == "New\n\nRefs #42"
    assert preserve_issue_links(body, "Closes #42") == (
        "- item\n\nCloses #42" if prefix.startswith("- item") else "Closes #42"
    )
    assert append_issue_link(body, 42, closes=False) == body
    assert append_issue_link(body, 42, closes=True) == f"{prefix}Closes #42\r\n"


@pytest.mark.parametrize("prefix", ["", "\\\\", "[link](https://example.com/`)\n"])
def test_code_example_does_not_mask_or_upgrade_a_real_link_with_the_same_text(prefix):
    example = f"{prefix}`example\nRefs #42\nexample`"
    body = f"{example}\nRefs #42"
    assert append_issue_link(body, 42, closes=True) == f"{example}\nCloses #42"
    assert preserve_issue_links("New", body) == "New\n\nRefs #42"


@pytest.mark.parametrize(
    "url", ["[link](https://example.com/`)", "<https://example.com/`>"]
)
def test_backticks_in_urls_do_not_mask_standalone_links(url):
    body = f"{url}\nRefs #42\n`"
    assert preserve_issue_links("New", body) == "New\n\nRefs #42"
    assert append_issue_link(body, 42, closes=True) == f"{url}\nCloses #42\n`"


# --------------------------------------------------------------------------- #
#   The patrol's notices                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("language", "expected_phrases", "dropped_phrase"),
    [
        (
            "en",
            (
                "converted it to a draft",
                "conversation comments",
                "review bodies",
                "Ready for review",
                "starts over",
                "through the chat",
                "interactive session",
            ),
            "request my review",
        ),
        (
            "ja",
            (
                "Draft にしました",
                "会話コメント",
                "review の本文",
                "Ready for review",
                "数え直し",
                "チャット",
                "対話セッション",
            ),
            "review request",
        ),
    ],
)
def test_review_limit_notice_explains_how_to_restart(
    language, expected_phrases, dropped_phrase
):
    set_language(language)
    try:
        message = t("integrations.pull_request_patrol.review_limit_reached", count=3)
    finally:
        set_language("en")

    assert all(phrase in message for phrase in expected_phrases)
    assert dropped_phrase not in message
