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
from guildbotics.runtime.code_hosting_resources import (
    RESOURCES_BY_SUBJECT,
    RepositoryReadError,
)
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
        created.issue_url, "comment", written.comment_id or 0, "eyes"
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
        await host.code.add_reaction(host.theirs.url or "", "comment", 1, "eyes")
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
    conversation = await code.read(
        "pull_request_comments", REPO, identifier=str(created.pr_number)
    )
    assert [c["id"] for c in conversation.items] == [written.comment_id]
    reactions = [
        await code.add_reaction(created.pr_url, target, comment_id or 0, "eyes")
        for target, comment_id in (
            ("comment", written.comment_id),
            ("review-comment", inline.review_comment_id),
            ("review", review.review_id),
        )
    ]
    assert [reaction.content for reaction in reactions] == ["eyes"] * 3


async def _two_pull_requests(code: CodeHostingService, host: Harness):
    created = []
    for branch in ("feature", "other"):
        host.branch(branch)
        created.append(
            await code.create_pull_request(
                REPO,
                branch,
                "main",
                branch,
                "",
                draft=False,
                issue_url="",
                closes_issue=False,
            )
        )
    return created


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["issue", "pull_request", "review-comment", "review"])
async def test_a_reaction_reaches_only_a_comment_of_the_item_it_names(
    host: Harness, kind: str
):
    """Comments are named inside the issue or pull request ``url`` names: an id
    of another item of the same repository is refused before anything is
    written."""
    code = host.code
    if kind == "issue":
        first, second = [
            (await code.create_issue(REPO, title, "", [])).issue_url
            for title in ("A", "B")
        ]
        written = await code.comment(first, "Hi", kind="issue")
        target, comment_id = "comment", written.comment_id
    else:
        first_pr, second_pr = await _two_pull_requests(code, host)
        first, second = first_pr.pr_url, second_pr.pr_url
        if kind == "pull_request":
            written = await code.comment(first, "Hi", kind="pull_request")
            target, comment_id = "comment", written.comment_id
        elif kind == "review":
            review = await code.review(first, "Looks good.", "approve")
            target, comment_id = "review", review.review_id
        else:
            inline = await code.review_comment(
                first, "Rename this.", "a.py", 3, "RIGHT", None, None
            )
            target, comment_id = "review-comment", inline.review_comment_id
    before = len(host.writes)

    with pytest.raises(MemberCapabilityError):
        await code.add_reaction(second, target, comment_id or 0, "eyes")

    assert len(host.writes) == before
    reaction = await code.add_reaction(first, target, comment_id or 0, "eyes")
    assert reaction.comment_id == comment_id


def _as(host: Harness, kind: str, number: int) -> str:
    """The URL of ``kind`` ``number`` of the repository."""
    owner, name = REPO.split("/")
    return host.url(owner, name, kind, number)


@pytest.mark.asyncio
async def test_an_issue_operation_reaches_no_pull_request(host: Harness):
    """An issue's URL names an issue only: one with a pull request's number is
    refused before anything is written, whether the code host numbers both
    together or each on its own."""
    code = host.code
    [created, _] = await _two_pull_requests(code, host)
    written = await code.comment(created.pr_url, "Hi", kind="pull_request")
    issue_url = _as(host, "issue", created.pr_number)
    before = len(host.writes)

    for kind in ("issue", None):
        with pytest.raises(MemberCapabilityError):
            await code.comment(issue_url, "Hi", kind=kind)
    with pytest.raises(MemberCapabilityError):
        await code.linked_pull_requests(issue_url)
    with pytest.raises(MemberCapabilityError):
        await code.update_issue(
            issue_url,
            body=None,
            title="Renamed",
            add_labels=[],
            remove_labels=[],
            state="closed",
            state_reason=None,
        )
    with pytest.raises(MemberCapabilityError):
        await code.add_reaction(issue_url, "comment", written.comment_id or 0, "eyes")

    assert len(host.writes) == before


@pytest.mark.asyncio
async def test_a_pull_request_links_only_an_issue_of_its_repository(host: Harness):
    code = host.code
    [created, other] = await _two_pull_requests(code, host)
    host.branch("third")
    before = len(host.writes)

    for issue_url in (
        _as(host, "issue", created.pr_number),
        other.pr_url,
        host.theirs.url or "",
    ):
        with pytest.raises(MemberCapabilityError):
            await code.create_pull_request(
                REPO,
                "third",
                "main",
                "Third",
                "",
                draft=False,
                issue_url=issue_url,
                closes_issue=True,
            )

    assert len(host.writes) == before


#: Every resource a code host reads of one issue or one pull request, but
#: those GitHub answers through GraphQL (its schema types issue and pull
#: request apart), which the double does not serve.
_ITEM_RESOURCES = [
    (subject, resource)
    for subject in ("issue", "pull_request")
    for resource in RESOURCES_BY_SUBJECT[subject]
    if resource
    not in {"issue_projects", "pull_request_threads", "review_thread_comments"}
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "resource"), _ITEM_RESOURCES)
async def test_a_resource_is_read_only_of_an_item_of_its_subject(
    host: Harness, subject: str, resource: str
):
    """An issue's resource is read of an issue and a pull request's of a pull
    request: the other kind's number reads nothing."""
    code = host.code
    if subject == "issue":
        [created, _] = await _two_pull_requests(code, host)
        number = created.pr_number
    else:
        number = (await code.create_issue(REPO, "Only", "", [])).issue_number

    with pytest.raises(RepositoryReadError):
        await code.read(resource, REPO, identifier=str(number))


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
