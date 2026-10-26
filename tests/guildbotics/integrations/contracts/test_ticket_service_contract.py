"""Every ticket service of the factory keeps the same contract.

The same steps run against each provider: a ticket the member writes reads
back the same way, named by its URL and the ids its reads give; a ticket
assigned to the member in the ready lane is a candidate, moves to the working
lane, and is re-read as work still to do there; a created ticket is on the
board unless asked otherwise, which a board holding every ticket refuses;
the board's closed work is listed for the activity history. A write outside
the configured scope -- a ticket, a comment, a reaction, a lane -- is refused
before it is made.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.runtime.port import ServiceError, ServiceNotFound, ServiceUnsupported
from guildbotics.runtime.ticket_service import TicketService
from tests.guildbotics.integrations.contracts.providers import (
    LABEL,
    Harness,
    harness,
)

#: The port's operations, each of which every ticket service answers.
_OPERATIONS = sorted(
    name
    for name, member in vars(TicketService).items()
    if getattr(member, "__isabstractmethod__", False)
)


@pytest_asyncio.fixture(
    params=sorted(name for name, p in PROVIDERS.items() if p.ticket_service)
)
async def board(request, tmp_path, monkeypatch):
    provided = harness(request.param, tmp_path, monkeypatch)
    yield provided
    await provided.aclose()


def test_every_ticket_service_answers_every_operation_of_the_port(board: Harness):
    implementation = type(board.tickets)
    assert [
        operation
        for operation in _OPERATIONS
        if getattr(implementation, operation) is getattr(TicketService, operation)
    ] == []


async def _ticket(board: Harness, title: str = "Fix login") -> str:
    return (await board.tickets.create_ticket(board.tracker, title, "", [])).target.url


@pytest.mark.asyncio
@pytest.mark.parametrize("uppercase", [False, True])
async def test_unassigned_mentions_are_answered_once_without_taking_the_ticket(
    board: Harness,
    uppercase,
):
    url = await _ticket(board)
    mention = board.mention.upper() if uppercase else board.mention
    board.human_comment(url, f"{mention} Please provide context.")
    before = await _read(board, url)

    [task] = await board.tickets.candidates()
    assert (task.url, task.assignee, task.trigger_reason) == (
        url,
        None,
        "issue_mention",
    )
    assert (await board.tickets.refresh(task)).trigger_reason == "issue_mention"
    assert await _read(board, url) == before

    await board.tickets.comment(url, "Here is the context.")
    board.human_comment(url, "Thanks.")
    assert await board.tickets.candidates() == []
    assert await board.tickets.refresh(task) is None
    after = await _read(board, url)
    assert (after["lane"], after["assignees"]) == (before["lane"], before["assignees"])


@pytest.mark.asyncio
@pytest.mark.parametrize("working", [False, True])
async def test_comments_addressed_to_other_members_do_not_wake_the_assignee(
    board: Harness, working
):
    url = await _ticket(board)
    board.assign(url)
    [task] = await board.tickets.candidates()
    if working:
        await board.tickets.move(task, "in_progress")
    await board.tickets.comment(url, "Please ask the author for context.")
    board.human_comment(url, "@OTOTA Please provide the context.")
    assert await board.tickets.candidates() == []
    assert await board.tickets.refresh(task) is None

    board.human_comment(url, f"{board.mention} Here is the context.")
    [selected] = await board.tickets.candidates()
    assert selected.assignee is not None
    assert selected.trigger_reason == ("issue_comment" if working else "ready_lane")
    assert await board.tickets.refresh(selected) is not None


@pytest.mark.asyncio
async def test_assignment_refreshes_mention_work_into_regular_work_without_duplicates(
    board: Harness,
):
    url = await _ticket(board)
    board.human_comment(url, f"{board.mention} Context?")
    [mention] = await board.tickets.candidates()
    board.assign(url)
    [assigned] = await board.tickets.candidates()
    assert assigned.trigger_reason == "ready_lane"
    refreshed = await board.tickets.refresh(mention)
    assert refreshed.assignee == assigned.assignee
    assert refreshed.trigger_reason == "ready_lane"


async def _relate(board: Harness, url: str, **relations) -> None:
    """Change only the relations of ticket ``url``."""
    await board.tickets.update_ticket(
        url,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(),
        state=None,
        state_reason=None,
        **relations,
    )


async def _read(board: Harness, url: str) -> dict:
    return (await board.tickets.read("tickets", url)).items[0]


@pytest.mark.asyncio
async def test_a_ticket_reads_back_as_it_was_written(board: Harness):
    tickets = board.tickets
    created = await tickets.create_ticket(
        board.tracker, "Fix login", "It fails.", [LABEL]
    )
    url = created.target.url

    updated = await tickets.update_ticket(
        url,
        body="It still fails.",
        title="Fix the login",
        add_labels=(),
        # One the ticket does not carry is removed already.
        remove_labels=("missing",),
        state=None,
        state_reason=None,
    )
    written = await tickets.comment(url, "Looking into it.")
    page = await tickets.read("tickets", url)
    comments = await tickets.read("ticket_comments", url)

    assert (created.target.kind, created.target.title, created.labels) == (
        "ticket",
        "Fix login",
        [LABEL],
    )
    assert (updated.target.title, updated.body, updated.state_changed) == (
        "Fix the login",
        "It still fails.",
        False,
    )
    [ticket] = page.items
    assert page.target == updated.target == written.target
    assert (ticket["title"], ticket["body"], ticket["state"], ticket["labels"]) == (
        "Fix the login",
        "It still fails.",
        "open",
        [LABEL],
    )
    assert (ticket["tracker"], ticket["repository"], ticket["lane"]) == (
        board.tracker,
        board.repository,
        "new",
    )
    assert ticket["key"] and ticket["label"] == created.target.label
    assert [(c["id"], c["body"], c["author_type"]) for c in comments.items] == [
        (written.id, "Looking into it.", Message.ASSISTANT)
    ]
    reaction = await tickets.add_reaction(url, written.id, "support")
    assert (reaction.reaction, reaction.id) == ("support", written.id)
    removed = await tickets.update_ticket(
        url,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(LABEL,),
        state=None,
        state_reason=None,
    )
    assert removed.labels == []


@pytest.mark.asyncio
async def test_a_ticket_closes_once(board: Harness):
    url = await _ticket(board, "Done soon")
    close = {
        "body": None,
        "title": None,
        "add_labels": (),
        "remove_labels": (),
        "state": "closed",
        "state_reason": "completed",
    }

    first = await board.tickets.update_ticket(url, **close)
    again = await board.tickets.update_ticket(url, **close)

    assert (first.state, first.state_changed) == ("closed", True)
    assert (again.state, again.state_changed) == ("closed", False)


@pytest.mark.asyncio
async def test_an_assigned_ready_ticket_is_taken_and_moved_to_the_working_lane(
    board: Harness,
):
    url = await _ticket(board, "Fix login")
    await _ticket(board, "Not mine")
    board.assign(url)

    [task] = await board.tickets.candidates()

    assert (task.url, task.status, task.trigger_reason) == (
        url,
        Task.READY,
        "ready_lane",
    )
    assert (await board.tickets.read("tickets", url)).items[0]["lane"] == "ready"
    assert await board.tickets.move(task, "in_progress") is True
    assert (await board.tickets.read("tickets", url)).items[0]["lane"] == "in_progress"
    refreshed = await board.tickets.refresh(task)
    assert refreshed is not None
    assert (refreshed.status, refreshed.trigger_reason) == (
        Task.IN_PROGRESS,
        "working_lane",
    )

    # The member's own word is an answer: the ticket waits for someone else.
    await board.tickets.comment(url, "On it.")
    assert await board.tickets.refresh(task) is None


@pytest.mark.asyncio
async def test_relations_read_back_from_both_sides_and_are_taken_back(board: Harness):
    parent, child, blocker = [
        await _ticket(board, title) for title in ("Parent", "Child", "Blocker")
    ]

    await _relate(board, child, parent=parent)
    await _relate(board, parent, add_blocked_by=[blocker])
    # Adding one it is blocked by already changes nothing, as labels do.
    await _relate(board, parent, add_blocked_by=[blocker])

    read = await _read(board, parent)
    assert [
        (c["kind"], c["url"], c["title"], c["state"]) for c in read["children"]
    ] == [("ticket", child, "Child", "open")]
    assert [(b["url"], b["state"]) for b in read["blocked_by"]] == [(blocker, "open")]
    assert (read["parent"], read["blocks"]) == (None, [])
    assert (await _read(board, child))["parent"]["url"] == parent
    assert [b["url"] for b in (await _read(board, blocker))["blocks"]] == [parent]

    await _relate(board, child, parent="")
    await _relate(board, parent, remove_blocked_by=[blocker])

    read = await _read(board, parent)
    assert (read["children"], read["blocked_by"]) == ([], [])
    assert (await _read(board, child))["parent"] is None
    assert (await _read(board, blocker))["blocks"] == []


@pytest.mark.asyncio
async def test_a_ready_ticket_is_not_taken_while_a_ticket_it_is_blocked_by_is_open(
    board: Harness,
):
    url, blocker = await _ticket(board, "Blocked"), await _ticket(board, "Blocker")
    await _relate(board, url, add_blocked_by=[blocker])
    board.assign(url)

    assert await board.tickets.candidates() == []

    await board.tickets.update_ticket(
        blocker,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(),
        state="closed",
        state_reason="not_planned",
    )
    assert [task.url for task in await board.tickets.candidates()] == [url]


@pytest.mark.asyncio
async def test_a_created_ticket_is_on_the_board_without_being_assigned(
    board: Harness,
):
    url = await _ticket(board, "Later")

    assert (await board.tickets.read("tickets", url)).items[0]["lane"] == "new"
    assert await board.tickets.candidates() == []


@pytest.mark.asyncio
async def test_a_ticket_is_created_off_the_board_or_refused_as_unsupported(
    board: Harness,
):
    before = len(board.writes)

    if "off_board" in board.features:
        created = await board.tickets.create_ticket(
            board.tracker, "Loose", "", [], add_to_board=False
        )
        [ticket] = (await board.tickets.read("tickets", created.target.url)).items
        assert ticket["lane"] is None
    else:
        with pytest.raises(ServiceUnsupported):
            await board.tickets.create_ticket(
                board.tracker, "Loose", "", [], add_to_board=False
            )
        assert board.writes[before:] == []


@pytest.mark.asyncio
async def test_the_boards_closed_work_is_listed(board: Harness):
    closed = await board.tickets.create_ticket(board.tracker, "Done", "", [])
    still_open = await _ticket(board, "Open")
    for url in (closed.target.url, still_open):
        board.assign(url)
    await board.tickets.update_ticket(
        closed.target.url,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(),
        state="closed",
        state_reason="completed",
    )

    items = await board.tickets.closed_since(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )

    assert [(item.kind, item.url, item.label, item.merged) for item in items] == [
        ("ticket", closed.target.url, closed.target.label, False)
    ]
    assert (
        await board.tickets.closed_since(
            datetime(2000, 1, 1, tzinfo=UTC), datetime(2001, 1, 1, tzinfo=UTC)
        )
        == []
    )


@pytest.mark.asyncio
async def test_writes_outside_the_configured_scope_are_refused(board: Harness):
    """Another scope's ticket is readable, and nothing of it is the member's
    to write: not a ticket, a comment, a reaction, or a lane."""
    theirs = board.theirs.url or ""
    mine = await _ticket(board, "Mine")
    before = len(board.writes)

    with pytest.raises(ServiceError, match="refused"):
        await board.tickets.create_ticket(board.their_tracker, "T", "B", [])
    with pytest.raises(ServiceError, match="refused"):
        await board.tickets.comment(theirs, "Hi")
    with pytest.raises(ServiceError, match="refused"):
        await board.tickets.add_reaction(theirs, board.their_comment, "ack")
    with pytest.raises(ServiceError, match="refused"):
        await board.tickets.move(board.theirs, "in_progress")
    # Nor is a relation of the member's ticket to it, refused by the scope
    # before anything is sent (a service may refuse it too, in other words).
    with pytest.raises(ServiceError, match="Writes are limited to"):
        await _relate(board, mine, parent=theirs)
    with pytest.raises(ServiceError, match="Writes are limited to"):
        await _relate(board, mine, add_blocked_by=[theirs])
    with pytest.raises(ServiceError, match="Writes are limited to"):
        await _relate(board, mine, remove_blocked_by=[theirs])
    # Nor a relation of it to the member's ticket, which would change it.
    for relation in (
        {"parent": mine},
        {"parent": ""},
        {"add_blocked_by": [mine]},
        {"remove_blocked_by": [mine]},
    ):
        with pytest.raises(ServiceError, match="Writes are limited to"):
            await _relate(board, theirs, **relation)

    assert board.writes[before:] == []
    assert (await board.tickets.read("tickets", theirs)).items[0]["title"] == "Theirs"
    # The writes that are the member's do reach the provider.
    await _ticket(board)
    assert board.writes[before:] != []


@pytest.mark.asyncio
async def test_a_reaction_reaches_only_a_comment_of_the_ticket_it_names(
    board: Harness,
):
    first, second = await _ticket(board, "A"), await _ticket(board, "B")
    written = await board.tickets.comment(first, "Hi")
    before = len(board.writes)

    with pytest.raises(ServiceNotFound):
        await board.tickets.add_reaction(second, written.id, "ack")

    assert board.writes[before:] == []
    assert (await board.tickets.add_reaction(first, written.id, "ack")).id == written.id


@pytest.mark.asyncio
async def test_a_reaction_of_no_meaning_is_refused_before_anything_is_written(
    board: Harness,
):
    url = await _ticket(board)
    written = await board.tickets.comment(url, "Hi")
    before = len(board.writes)

    with pytest.raises(ServiceUnsupported):
        await board.tickets.add_reaction(url, written.id, "eyes")

    assert board.writes[before:] == []


@pytest.mark.asyncio
async def test_a_ticket_operation_reaches_no_pull_request(board: Harness):
    """A pull request's URL names no ticket, whatever the service numbers or
    spells: each operation refuses it before anything is written."""
    board.branch("feature")
    pull = await board.code.create_pull_request(
        board.repository,
        "feature",
        "main",
        "Feature",
        "",
        draft=False,
        tickets=[],
    )
    url = pull.target.url
    written = await board.code.comment(url, "Hi")
    before = len(board.writes)

    with pytest.raises(ServiceNotFound):
        await board.tickets.comment(url, "Hi")
    with pytest.raises(ServiceNotFound):
        await board.tickets.add_reaction(url, written.id, "ack")
    with pytest.raises(ServiceNotFound):
        await board.tickets.update_ticket(
            url,
            body=None,
            title="Renamed",
            add_labels=[],
            remove_labels=[],
            state="closed",
            state_reason=None,
        )
    for resource in ("tickets", "ticket_comments"):
        with pytest.raises(ServiceNotFound):
            await board.tickets.read(resource, url)

    assert board.writes[before:] == []


@pytest.mark.asyncio
async def test_a_label_the_tracker_does_not_define_is_refused(board: Harness):
    url = await _ticket(board)
    before = len(board.writes)

    with pytest.raises(ServiceError, match="not defined"):
        await board.tickets.update_ticket(
            url,
            body=None,
            title=None,
            add_labels=("invented",),
            remove_labels=(),
            state=None,
            state_reason=None,
        )

    assert board.writes[before:] == []
