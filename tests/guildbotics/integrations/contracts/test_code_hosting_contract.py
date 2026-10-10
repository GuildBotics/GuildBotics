"""Every code host of the factory keeps the same contract.

The same steps run against each provider: what a member writes reads back
the same way, a write outside the configured owner is refused before it is
made, a pull request whose head is current with passing CI is ready (and one
whose base cannot be read is not judged at all), and the git remote names the
repository it is the remote of.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from guildbotics.entities.message import Message
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.integrations.repository_scope import RepositoryScopeError
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.integration_factory import MemberCapabilityError
from tests.guildbotics.integrations.contracts.providers import (
    OWNER,
    REPO,
    THEIRS,
    Harness,
    harness,
)

#: The port's operations, each of which every code host answers.
_OPERATIONS = sorted(
    name
    for name, member in vars(CodeHostingService).items()
    if callable(member) and not name.startswith("_")
)


@pytest_asyncio.fixture(
    params=sorted(name for name, p in PROVIDERS.items() if p.code_hosting)
)
async def host(request, tmp_path, monkeypatch):
    provided = harness(request.param, tmp_path, monkeypatch)
    yield provided
    await provided.aclose()


def test_every_code_host_answers_every_operation_of_the_port(host: Harness):
    implementation = type(host.code)
    assert [
        operation
        for operation in _OPERATIONS
        if getattr(implementation, operation) is getattr(CodeHostingService, operation)
    ] == []


@pytest.mark.asyncio
async def test_an_issue_reads_back_as_it_was_written(host: Harness):
    code = host.code
    created = await code.create_issue(REPO, "Fix login", "It fails.", [])
    assert (created.repo, created.issue_title) == (REPO, "Fix login")
    assert (
        created.target.kind == "issue" and created.target.html_url == created.issue_url
    )

    updated = await code.update_issue(
        created.issue_url,
        body="It still fails.",
        title="Fix the login",
        add_labels=(),
        remove_labels=(),
        state=None,
        state_reason=None,
    )
    written = await code.comment(created.issue_url, "Looking into it.", kind="issue")
    page = await code.read("issues", REPO, identifier=str(created.issue_number))
    comments = await code.read(
        "issue_comments", REPO, identifier=str(created.issue_number)
    )

    assert (updated.title, updated.body, updated.state_changed) == (
        "Fix the login",
        "It still fails.",
        False,
    )
    [issue] = page.items
    assert (issue["title"], issue["body"], issue["state"]) == (
        "Fix the login",
        "It still fails.",
        "open",
    )
    assert page.target == updated.target
    assert written.target.number == created.issue_number
    assert [(c["id"], c["body"], c["author_type"]) for c in comments.items] == [
        (written.comment_id, "Looking into it.", Message.ASSISTANT)
    ]
    reaction = await code.add_reaction(
        REPO, "issue-comment", written.comment_id or 0, "eyes"
    )
    assert (reaction.content, reaction.comment_id) == ("eyes", written.comment_id)


@pytest.mark.asyncio
async def test_an_issue_closes_once(host: Harness):
    created = await host.code.create_issue(REPO, "Done soon", "", [])
    close = {
        "body": None,
        "title": None,
        "add_labels": (),
        "remove_labels": (),
        "state": "closed",
        "state_reason": "completed",
    }

    first = await host.code.update_issue(created.issue_url, **close)
    again = await host.code.update_issue(created.issue_url, **close)

    assert (first.state, first.state_changed) == ("closed", True)
    assert (again.state, again.state_changed) == ("closed", False)


@pytest.mark.asyncio
async def test_writes_outside_the_configured_owner_are_refused(host: Harness):
    elsewhere = THEIRS
    before = len(host.writes)

    with pytest.raises(RepositoryScopeError, match=f"'{OWNER}'"):
        await host.code.create_issue(elsewhere, "T", "B", [])
    with pytest.raises(RepositoryScopeError, match=f"'{OWNER}'"):
        await host.code.create_pull_request(
            elsewhere,
            "feature",
            "main",
            "T",
            "B",
            draft=False,
            issue_url="",
            closes_issue=False,
        )
    with pytest.raises(RepositoryScopeError, match=f"'{OWNER}'"):
        await host.code.add_reaction(elsewhere, "issue-comment", 1, "eyes")
    with pytest.raises(RepositoryScopeError, match=f"'{OWNER}'"):
        await host.code.comment(host.theirs.url or "", "Hi")

    # None of them reached the provider; reads are not limited by owner.
    assert host.writes[before:] == []
    await host.code.create_issue(REPO, "Ours", "", [])
    assert len(host.writes) > before


@pytest.mark.asyncio
async def test_a_pull_request_with_a_current_head_and_passing_ci_is_ready(
    host: Harness,
):
    code = host.code
    issue = await code.create_issue(REPO, "Fix login", "", [])
    host.branch("ticket/1")

    created = await code.create_pull_request(
        REPO,
        "ticket/1",
        "",
        "Fix login",
        "Body",
        draft=False,
        issue_url=issue.issue_url,
        closes_issue=True,
    )
    again = await code.create_pull_request(
        REPO,
        "ticket/1",
        "main",
        "Fix login",
        "Body",
        draft=False,
        issue_url=issue.issue_url,
        closes_issue=True,
    )
    readiness = await code.readiness(created.pr_url)
    page = await code.read("pull_requests", REPO, identifier=str(created.pr_number))

    assert (created.created, again.created) == (True, False)
    assert (created.base, again.pr_number) == ("main", created.pr_number)
    assert readiness.readiness == "ready" and readiness.completion_blockers == []
    assert readiness.target == created.target
    [pull] = page.items
    assert (pull["head"], pull["base"], pull["author_type"]) == (
        "ticket/1",
        "main",
        Message.ASSISTANT,
    )
    assert code.locate(created.pr_url, "pull_request").url == created.pr_url


@pytest.mark.asyncio
async def test_a_pull_request_whose_base_is_gone_is_not_judged(host: Harness):
    """Readiness answers only from a base it read: a base it cannot read is no
    base without commits ahead of the head."""
    host.branch("release")
    host.branch("ticket/1")
    created = await host.code.create_pull_request(
        REPO,
        "ticket/1",
        "release",
        "Fix",
        "",
        draft=False,
        issue_url="",
        closes_issue=False,
    )
    host.delete_branch("release")
    host.branch("release/next")  # a branch below the base's name is not the base

    # GitHub's client raises the HTTP error its response hook makes.
    with pytest.raises((MemberCapabilityError, httpx.HTTPStatusError)):
        await host.code.readiness(created.pr_url)


@pytest.mark.asyncio
async def test_a_pull_request_is_reviewed_and_its_threads_replied_to(host: Harness):
    code = host.code
    host.branch("feature")
    created = await code.create_pull_request(
        REPO,
        "feature",
        "main",
        "Feature",
        "",
        draft=False,
        issue_url="",
        closes_issue=False,
    )

    review = await code.review(created.pr_url, "Looks good.", "approve")
    inline = await code.review_comment(
        created.pr_url, "Rename this.", "a.py", 3, "RIGHT", None, None
    )
    reply = await code.reply(created.pr_url, inline.review_comment_id or 0, "Done.")
    written = await code.comment(created.pr_url, "Thanks!", kind="pull_request")

    assert review.commit_id == (await code.readiness(created.pr_url)).head_sha
    assert (inline.path, inline.line, inline.side) == ("a.py", 3, "RIGHT")
    assert reply.reply_comment_id not in (None, inline.review_comment_id)
    assert [review.target, inline.target, reply.target, written.target] == [
        created.target
    ] * 4


@pytest.mark.asyncio
async def test_the_git_remote_names_its_repository_and_its_open_pull_requests(
    host: Harness,
):
    code = host.code
    host.branch("feature")
    created = await code.create_pull_request(
        REPO,
        "feature",
        "main",
        "Feature",
        "",
        draft=False,
        issue_url="",
        closes_issue=False,
    )
    owner, repo = REPO.split("/")
    remote = await code.clone_url(owner, repo)

    assert code.repository_from_remote(remote) == (owner, repo)
    assert await code.default_branch(owner, repo) == "main"
    assert await code.open_pull_requests(remote, "feature") == [created.pr_url]
    assert await code.open_pull_requests(remote, "other") == []
    head = await code.pull_request_head(created.pr_url)
    assert (head.full_repo, head.branch) == (REPO, "feature")
    other = host.url("other-owner", "demo", "issue", 1)
    assert code.locate(other).full_repo == "other-owner/demo"
