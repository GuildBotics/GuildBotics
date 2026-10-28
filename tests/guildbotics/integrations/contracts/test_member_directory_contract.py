"""Service actors and requests resolve through each provider's public roster."""

from typing import get_args, get_origin

import pytest
import pytest_asyncio

from guildbotics.entities import Person
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.runtime.resources import ActorName, RESOURCES
from tests.guildbotics.integrations.contracts.providers import harness


@pytest_asyncio.fixture(
    params=sorted(
        name for name, provider in PROVIDERS.items() if provider.code_hosting_service
    )
)
async def host(request, tmp_path, monkeypatch):
    provided = harness(request.param, tmp_path, monkeypatch)
    yield provided
    await provided.aclose()


def actors(resource, item):
    """Enumerate people by their schema type, including nested thread comments."""
    for field, definition in RESOURCES[resource].model_fields.items():
        annotation = definition.annotation
        if annotation is ActorName:
            yield item[field]
        elif get_origin(annotation) is list and get_args(annotation) == (ActorName,):
            yield from item[field]
        elif resource == "pull_request_threads" and field == "comments":
            for comment in item[field]:
                yield from actors("review_thread_comments", comment)


def resolves(host, resource, item, expected):
    names = list(actors(resource, item))
    assert names
    provider = PROVIDERS[host.name]
    for name in names:
        assert [
            member.person_id
            for member in host.team.members
            if provider.member_identity(member).matches(name)
        ] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_every_actor_field_resolves_when_another_member_reads(host, reverse):
    first, second = host.team.members[::-1] if reverse else host.team.members
    writer_code, reader_code = (
        (host.other_code, host.code) if reverse else (host.code, host.other_code)
    )
    writer_tickets, reader_tickets = (
        (host.other_tickets, host.tickets)
        if reverse
        else (host.tickets, host.other_tickets)
    )
    provider = PROVIDERS[host.name]
    assert all(
        provider.member_identity(member).name != member.person_id
        for member in (first, second)
    )
    ticket = await writer_tickets.create_ticket(host.tracker, "Context", "", [])
    host.assign_to(ticket.target.url, first)
    await writer_tickets.comment(ticket.target.url, "Details")
    for resource in ("tickets", "ticket_comments"):
        page = await reader_tickets.read(resource, ticket.target.url)
        resolves(host, resource, page.items[0], first.person_id)

    host.branch("feature")
    created = await writer_code.create_pull_request(
        host.repository, "feature", "main", "Fix", "", draft=False, tickets=[]
    )
    url = created.target.url
    host.request_review(url, first)
    pull = (await reader_code.read("pull_requests", url)).items[0]
    resolves(host, "pull_requests", pull, first.person_id)
    assert pull["author_type"] == "User"
    head = pull["head_sha"]
    # A comment verdict and inline notes are valid on one's own PR too.
    await writer_code.comment(url, "Design context")
    await writer_code.review(url, "Review context", "comment", head)
    # The hosted doubles and local seed each expose their actual changed file.
    files = (await writer_code.read("pull_request_files", url)).items
    assert files
    file = files[0]
    coordinate = file["commentable_lines"][0]
    await writer_code.review_comment(
        url,
        "Inline context",
        head,
        file["path"],
        coordinate["line"],
        coordinate["side"],
        None,
        None,
    )
    for resource in (
        "pull_request_comments",
        "pull_request_reviews",
        "pull_request_threads",
    ):
        page = await reader_code.read(resource, url)
        assert page.items
        for item in page.items:
            resolves(host, resource, item, first.person_id)
        if resource == "pull_request_threads":
            thread = page.items[0]
            await writer_code.reply(url, thread["comments"][0]["id"], "Further context")
            page = await reader_code.read(
                "review_thread_comments", url, parameters={"node": thread["id"]}
            )
            resolves(host, "review_thread_comments", page.items[0], first.person_id)


@pytest.mark.asyncio
async def test_roster_mentions_and_assignments_are_the_patrols_requests(host):
    first, second = host.team.members
    provider = PROVIDERS[host.name]
    for person, reader in ((first, host.tickets), (second, host.other_tickets)):
        ticket = await host.tickets.create_ticket(host.tracker, "Question", "", [])
        await host.tickets.comment(
            ticket.target.url,
            f"{provider.member_identity(person).mention} Please answer.",
        )
        # A cannot summon itself: have B post A's request.
        if person is first:
            await host.other_tickets.comment(
                ticket.target.url,
                f"{provider.member_identity(person).mention} Please answer.",
            )
        before = list(host.writes)
        assert await reader.can_receive_work(ticket.target.url)
        assert host.writes == before
        assert ticket.target.url in [task.url for task in await reader.candidates()]
        await reader.comment(ticket.target.url, "Answered.")
        assert not await reader.can_receive_work(ticket.target.url)
        assert ticket.target.url not in [task.url for task in await reader.candidates()]

    ticket = await host.tickets.create_ticket(host.tracker, "Assigned", "", [])
    host.assign_to(ticket.target.url, second)
    assigned = (await host.tickets.read("tickets", ticket.target.url)).items[0]
    resolves(host, "tickets", assigned, second.person_id)
    assert await host.other_tickets.can_receive_work(ticket.target.url)
    assert ticket.target.url in [
        task.url for task in await host.other_tickets.candidates()
    ]


@pytest.mark.asyncio
async def test_pr_readiness_for_dispatch_agrees_with_patrol_without_writes(host):
    host.branch("feature")
    created = await host.code.create_pull_request(
        host.repository, "feature", "main", "Fix", "", draft=False, tickets=[]
    )
    url = created.target.url
    # No request, review, or unanswered statement.
    before = list(host.writes)
    assert not await host.other_code.can_receive_work(url)
    assert host.writes == before
    assert url not in [
        task.pull_request_url
        for task in await host.other_code.pull_request_candidates()
    ]
    host.request_review(url, host.team.members[1])
    before = list(host.writes)
    assert await host.other_code.can_receive_work(url)
    assert host.writes == before
    assert url in [
        task.pull_request_url
        for task in await host.other_code.pull_request_candidates()
    ]
    host.merge(url)
    before = list(host.writes)
    assert not await host.other_code.can_receive_work(url)
    assert host.writes == before
    assert url not in [
        task.pull_request_url
        for task in await host.other_code.pull_request_candidates()
    ]


def test_actor_field_population_is_explicit():
    assert {
        (resource, field)
        for resource, schema in RESOURCES.items()
        for field, definition in schema.model_fields.items()
        if definition.annotation is ActorName
        or (
            get_origin(definition.annotation) is list
            and get_args(definition.annotation) == (ActorName,)
        )
    } == {
        ("tickets", "assignees"),
        ("ticket_comments", "author"),
        ("pull_requests", "assignees"),
        ("pull_requests", "author"),
        ("pull_requests", "requested_reviewers"),
        ("pull_request_comments", "author"),
        ("pull_request_reviews", "author"),
        ("review_thread_comments", "author"),
    }


@pytest.mark.parametrize(
    "provider", list(PROVIDERS.values()), ids=lambda provider: provider.name
)
def test_directory_needs_neither_credentials_nor_configured_accounts(
    provider, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Directory read attempted to use a credential")

    monkeypatch.setattr(Person, "get_secret", forbidden)
    for kind in ("human", "agent"):
        identity = provider.member_identity(
            Person(person_id="member", name="Member", person_type=kind)
        )
        assert not identity.matches("")
