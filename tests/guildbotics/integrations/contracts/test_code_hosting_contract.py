"""Every code host of the factory keeps the same contract.

The same steps run against each provider: what a member writes reads back
the same way and is named by the URL and the ids the reads give, a write
outside the configured scope is refused before it is made, a review or an
inline comment lands only on the head the member read, a merge closes every
ticket the pull request links to be closed by it, a pull request whose
head is current with passing CI is ready (and one whose base cannot be read
is not judged at all), and the git remote names the repository it is the
remote of. What a provider cannot do at all it refuses as unsupported.
"""

from __future__ import annotations

import io
import zipfile

import pytest
import pytest_asyncio

from guildbotics.entities.message import Message
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.runtime.code_hosting_service import CodeHostingService, TicketLink
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
)
from guildbotics.runtime.resources import RESOURCES_BY_SUBJECT, TICKET_RESOURCES
from tests.guildbotics.integrations.contracts.providers import (
    FOREIGN_TICKET,
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
    params=sorted(name for name, p in PROVIDERS.items() if p.code_hosting_service)
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


async def _pull_request(host: Harness, branch: str = "feature", **fields):
    host.branch(branch)
    return await host.code.create_pull_request(
        host.repository,
        branch,
        fields.pop("base", "main"),
        branch,
        fields.pop("body", ""),
        draft=False,
        tickets=fields.pop("tickets", []),
    )


async def _ticket(host: Harness, title: str = "Fix login") -> str:
    created = await host.tickets.create_ticket(host.tracker, title, "", [])
    return created.target.url


async def _head(host: Harness, url: str) -> str:
    return (await host.code.read("pull_requests", url)).items[0]["head_sha"]


@pytest.mark.asyncio
async def test_a_pull_request_with_a_current_head_and_passing_ci_is_ready(
    host: Harness,
):
    code = host.code
    ticket = await _ticket(host)
    host.branch("feature")

    created = await code.create_pull_request(
        host.repository,
        "feature",
        "",
        "Fix login",
        "Body",
        draft=False,
        tickets=[TicketLink(ticket, closes=True)],
    )
    again = await code.create_pull_request(
        host.repository,
        "feature",
        "main",
        "Fix login",
        "Body",
        draft=False,
        tickets=[TicketLink(ticket, closes=True)],
    )
    readiness = await code.readiness(created.target.url)
    page = await code.read("pull_requests", created.target.url)

    assert (created.created, again.created) == (True, False)
    assert (created.base, again.target) == ("main", created.target)
    assert (created.target.kind, created.target.title) == ("pull_request", "Fix login")
    assert created.target.label
    assert readiness.readiness == "ready" and readiness.completion_blockers == []
    assert readiness.target == created.target
    [pull] = page.items
    assert page.target == created.target
    assert (pull["head"], pull["base"], pull["author_type"]) == (
        "feature",
        "main",
        Message.ASSISTANT,
    )
    assert (pull["repository"], pull["head_repository"]) == (host.repository,) * 2
    referring = await code.read("ticket_pull_requests", ticket)
    assert [(p["url"], p["label"]) for p in referring.items] == [
        (created.target.url, created.target.label)
    ]


@pytest.mark.asyncio
async def test_a_pull_request_whose_base_is_gone_is_not_judged(host: Harness):
    """Readiness answers only from a base it read: a base it cannot read is no
    base without commits ahead of the head."""
    host.branch("release")
    created = await _pull_request(host, "feature", base="release")
    host.delete_branch("release")
    host.branch("release/next")  # a branch below the base's name is not the base

    with pytest.raises(ServiceError):
        await host.code.readiness(created.target.url)


@pytest.mark.asyncio
async def test_a_pull_request_is_reviewed_and_its_threads_replied_to(host: Harness):
    code = host.code
    created = await _pull_request(host)
    url = created.target.url
    head = await _head(host, url)

    review = await code.review(url, "Looks good.", "approve", head)
    inline = await code.review_comment(
        url, "Rename this.", head, "a.py", 3, "RIGHT", None, None
    )
    reply = await code.reply(url, inline.id, "Done.")
    written = await code.comment(url, "Thanks!")

    assert (review.verdict, review.commit) == ("approve", head)
    assert (inline.path, inline.line, inline.side) == ("a.py", 3, "RIGHT")
    assert reply.id not in (inline.id, written.id)
    assert [review.target, inline.target, reply.target, written.target] == [
        created.target
    ] * 4
    conversation = await code.read("pull_request_comments", url)
    reviews = await code.read("pull_request_reviews", url)
    assert [c["id"] for c in conversation.items] == [written.id]
    [read_review] = reviews.items
    assert (read_review["verdict"], read_review["commit"]) == ("approve", head)
    for target_id in (written.id, inline.id, read_review["id"]):
        reaction = await code.add_reaction(url, target_id, "ack")
        assert (reaction.reaction, reaction.id) == ("ack", target_id)


@pytest.mark.asyncio
async def test_a_verdict_lands_only_on_the_head_the_member_read(host: Harness):
    """A head that moved since the member read it has commits the member has
    not read: neither a verdict nor an inline comment is written on it."""
    created = await _pull_request(host)
    url = created.target.url
    read = await _head(host, url)
    host.advance("feature")
    before = len(host.writes)

    with pytest.raises(ServiceError, match="head"):
        await host.code.review(url, "Looks good.", "approve", read)
    with pytest.raises(ServiceError, match="head"):
        await host.code.review_comment(
            url, "Here.", read, "a.py", 1, "RIGHT", None, None
        )

    assert host.writes[before:] == []
    current = await _head(host, url)
    assert current != read
    assert (
        await host.code.review(url, "Looks good.", "approve", current)
    ).commit == current


@pytest.mark.asyncio
async def test_request_changes_is_given_or_refused_as_unsupported(host: Harness):
    created = await _pull_request(host)
    url = created.target.url
    head = await _head(host, url)

    if "request_changes" in host.features:
        submitted = await host.code.review(url, "Fix it.", "request_changes", head)
        assert submitted.verdict == "request_changes"
    else:
        with pytest.raises(ServiceUnsupported):
            await host.code.review(url, "Fix it.", "request_changes", head)


@pytest.mark.asyncio
async def test_writes_outside_the_configured_scope_are_refused(host: Harness):
    before = len(host.writes)

    with pytest.raises(ServiceError, match="refused"):
        await host.code.create_pull_request(
            host.elsewhere,
            "feature",
            "main",
            "T",
            "B",
            draft=False,
            tickets=[],
        )
    with pytest.raises(ServiceError, match="refused"):
        host.code.check_push(host.elsewhere)

    # None of them reached the provider; reads are not limited by scope.
    assert host.writes[before:] == []
    host.code.check_push(host.repository)
    # The writes that are the member's do reach the provider.
    await _pull_request(host)
    assert host.writes[before:] != []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["conversation", "inline", "review"])
async def test_a_reaction_reaches_only_what_the_item_it_names_holds(
    host: Harness, kind: str
):
    """A comment or review is named inside the pull request ``url`` names: an
    id of another pull request is refused before anything is written."""
    code = host.code
    first = (await _pull_request(host, "feature")).target.url
    second = (await _pull_request(host, "other")).target.url
    head = await _head(host, first)
    if kind == "conversation":
        target_id = (await code.comment(first, "Hi")).id
    elif kind == "inline":
        target_id = (
            await code.review_comment(
                first, "Here.", head, "a.py", 1, "RIGHT", None, None
            )
        ).id
    else:
        await code.review(first, "Looks good.", "approve", head)
        target_id = (await code.read("pull_request_reviews", first)).items[0]["id"]
    before = len(host.writes)

    with pytest.raises(ServiceNotFound):
        await code.add_reaction(second, target_id, "agree")
    with pytest.raises(ServiceNotFound):
        await code.reply(second, target_id, "Hi")

    assert host.writes[before:] == []
    assert (await code.add_reaction(first, target_id, "agree")).id == target_id


@pytest.mark.asyncio
async def test_a_pull_request_names_no_ticket(host: Harness):
    """A pull request's URL is no ticket for another pull request to link."""
    first = (await _pull_request(host, "feature")).target.url
    before = len(host.writes)

    for closes in (True, False):
        with pytest.raises(ServiceNotFound):
            await _pull_request(
                host, f"other-{closes}", tickets=[TicketLink(first, closes)]
            )
    with pytest.raises(ServiceNotFound):
        await host.code.read("ticket_pull_requests", first)

    assert host.writes[before:] == []


@pytest.mark.asyncio
async def test_a_reaction_of_no_meaning_is_refused_before_anything_is_written(
    host: Harness,
):
    url = (await _pull_request(host)).target.url
    written = await host.code.comment(url, "Hi")
    before = len(host.writes)

    with pytest.raises(ServiceUnsupported):
        await host.code.add_reaction(url, written.id, "eyes")

    assert host.writes[before:] == []


@pytest.mark.asyncio
async def test_a_pull_request_operation_reaches_no_ticket(host: Harness):
    """A ticket's URL names no pull request, whatever the service numbers or
    spells: each operation refuses it before anything is written."""
    code = host.code
    ticket = await _ticket(host)
    before = len(host.writes)

    with pytest.raises(ServiceNotFound):
        await code.comment(ticket, "Hi")
    with pytest.raises(ServiceNotFound):
        await code.review(ticket, "Hi", "approve", "sha")
    with pytest.raises(ServiceNotFound):
        await code.readiness(ticket)
    with pytest.raises(ServiceNotFound):
        await code.pull_request_head(ticket)

    assert host.writes[before:] == []


#: Every resource the code host reads of one pull request.
_PULL_REQUEST_RESOURCES = [
    resource
    for resource in RESOURCES_BY_SUBJECT["pull_request"]
    if resource not in {"pull_request_threads", "review_thread_comments"}
]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", _PULL_REQUEST_RESOURCES)
async def test_a_pull_requests_resource_is_read_only_of_a_pull_request(
    host: Harness, resource: str
):
    ticket = await _ticket(host)

    with pytest.raises(ServiceNotFound):
        await host.code.read(resource, ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", sorted(TICKET_RESOURCES))
async def test_a_tickets_own_resource_is_the_ticket_services(
    host: Harness, resource: str
):
    with pytest.raises(ServiceError):
        await host.code.read(resource, await _ticket(host))


@pytest.mark.asyncio
async def test_the_pull_requests_of_another_services_ticket_are_unsupported(
    host: Harness,
):
    with pytest.raises(ServiceUnsupported):
        await host.code.read("ticket_pull_requests", FOREIGN_TICKET)


@pytest.mark.asyncio
async def test_a_merge_closes_every_ticket_the_pull_request_closes(host: Harness):
    first, second, third = [await _ticket(host, title) for title in "ABC"]
    closing = await _pull_request(
        host,
        "feature",
        tickets=[TicketLink(first, closes=True), TicketLink(second, closes=True)],
    )
    referring = await _pull_request(
        host,
        "other",
        tickets=[TicketLink(third, closes=False), TicketLink(first, closes=False)],
    )

    for ticket, pulls in ((first, [closing, referring]), (second, [closing])):
        page = await host.code.read("ticket_pull_requests", ticket)
        assert sorted(p["url"] for p in page.items) == sorted(
            p.target.url for p in pulls
        )
    host.merge(referring.target.url)
    host.merge(closing.target.url)

    async def state(ticket: str) -> str:
        return (await host.tickets.read("tickets", ticket)).items[0]["state"]

    assert [await state(ticket) for ticket in (first, second, third)] == [
        "closed",
        "closed",
        "open",
    ]


@pytest.mark.asyncio
async def test_a_merge_closes_only_a_ticket_the_code_host_can_close(host: Harness):
    """One ticket the merge cannot close is enough to open nothing, however
    many it can."""
    own = TicketLink(await _ticket(host), closes=True)
    before = len(host.writes)

    with pytest.raises(ServiceUnsupported):
        await _pull_request(
            host, "feature", tickets=[own, TicketLink(FOREIGN_TICKET, closes=True)]
        )
    # A ticket of another repository, though this service's, is not the
    # merge's to close.
    with pytest.raises(ServiceUnsupported):
        await _pull_request(
            host, "other", tickets=[own, TicketLink(host.theirs.url, closes=True)]
        )
    with pytest.raises(ServiceNotFound):
        await _pull_request(
            host, "fourth", tickets=[own, TicketLink(host.missing_ticket, False)]
        )

    assert host.writes[before:] == []
    refers = await _pull_request(
        host, "third", tickets=[TicketLink(FOREIGN_TICKET, closes=False)]
    )
    assert refers.created


@pytest.mark.asyncio
async def test_an_artifact_is_only_the_current_heads(host: Harness):
    created = await _pull_request(host)
    url = created.target.url
    head = await _head(host, url)
    # An artifact is a zip archive, as CI uploads it.
    report = io.BytesIO()
    with zipfile.ZipFile(report, "w") as archive:
        archive.writestr("report.txt", "ok")
    host.artifact(head, "report", report.getvalue())

    async with host.code.artifact(url, "report", 1024) as (ref, archive):
        assert (ref.name, archive.read()) == ("report", report.getvalue())
    with pytest.raises(ServiceError):
        async with host.code.artifact(url, "report", 1):
            pass
    host.advance("feature")
    with pytest.raises(ServiceNotFound):
        async with host.code.artifact(url, "report", 1024):
            pass


@pytest.mark.asyncio
async def test_dependency_alerts_are_read_or_refused_as_unsupported(host: Harness):
    if "dependency_alerts" in host.features:
        page = await host.code.read("dependency_alerts", host.repository)
        assert page.items == []
    else:
        with pytest.raises(ServiceUnsupported):
            await host.code.read("dependency_alerts", host.repository)


@pytest.mark.asyncio
async def test_the_callers_git_environment_does_not_reach_the_service(
    host: Harness, monkeypatch, tmp_path
):
    """The member's git runs with variables naming its own repository and
    configuration; what the service answers of the remote does not follow
    them."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    remote = await host.code.clone_url(host.repository)

    assert host.code.repository_from_remote(remote) == host.repository
    assert await host.code.default_branch(host.repository) == "main"


@pytest.mark.asyncio
async def test_the_git_remote_names_its_repository_and_its_open_pull_requests(
    host: Harness,
):
    code = host.code
    created = await _pull_request(host)
    remote = await code.clone_url(host.repository)

    assert code.repository_from_remote(remote) == host.repository
    assert await code.default_branch(host.repository) == "main"
    assert await code.open_pull_requests(host.repository, "feature") == [
        created.target.url
    ]
    assert await code.open_pull_requests(host.repository, "other") == []
    head = await code.pull_request_head(created.target.url)
    assert (head.repository, head.branch) == (host.repository, "feature")
