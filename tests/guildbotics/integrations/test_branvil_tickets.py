"""Which tickets of the Branvil board are a member's work, beyond what the
ticket service contract asks: the member's own word, a summons, a held
status, the time of the assignment, the pull request that takes a ticket
over, and the patrol's order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.runtime.port import ServiceError, WorkflowStatus
from tests.guildbotics.integrations.branvil_double import (
    BOARD,
    MEMBER,
    OTHER_BOARD,
    REPO,
    TRACKER,
    BranvilDouble,
    services,
)


def _status(reason: str, **fields: str) -> dict[str, str]:
    return WorkflowStatus.model_validate(
        {"reason": reason, "person_id": MEMBER, "run_id": "r", **fields}
    ).payload()


def _ticket(double: BranvilDouble, body: str = "", lane: str = "ready") -> str:
    url = double._ticket(TRACKER, "Fix login", body, [])["url"]
    double.assign(url, lane)
    return url


async def _reasons(tickets) -> list[tuple[str | None, str | None]]:
    return [(task.url, task.trigger_reason) for task in await tickets.candidates()]


@pytest.mark.asyncio
async def test_a_ready_ticket_waits_after_the_members_word_until_someone_speaks(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    url = _ticket(double)
    double.comment(url, MEMBER, "On it.")
    assert await _reasons(tickets) == []

    double.comment(url, "otota", "Any news?")

    [task] = await tickets.candidates()
    assert (task.url, task.status, task.trigger_reason) == (
        url,
        Task.READY,
        "ready_lane",
    )
    assert (task.label, task.provider, task.assignee) == ("DEMO-1", "branvil", MEMBER)
    # Comments are the team's members', by name, or their author type.
    assert [(c.author, c.author_type) for c in task.comments] == [
        ("Aiko", Message.ASSISTANT),
        ("Otota", Message.USER),
    ]


@pytest.mark.asyncio
async def test_what_the_member_said_before_the_assignment_does_not_answer_it(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(TRACKER, "Fix login", "", [])["url"]
    double.comment(url, MEMBER, "I reported this.")
    double.assign(url)

    assert await _reasons(tickets) == [(url, "ready_lane")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        _status("failed"),
        _status("rate_limited", retry_after_at="2100-01-01T00:00:00+00:00"),
    ],
    ids=["failed", "rate_limited"],
)
async def test_the_members_status_holds_the_ticket_until_someone_speaks(
    monkeypatch, status
):
    double, _, tickets = services(monkeypatch)
    url = _ticket(double, "@aiko please", lane="in_progress")
    double.comment(url, MEMBER, "It failed.", **status)
    assert await _reasons(tickets) == []

    double.comment(url, "otota", "Try again.")

    assert await _reasons(tickets) == [(url, "issue_comment")]


@pytest.mark.asyncio
async def test_the_working_lane_takes_a_ticket_up_again_by_what_was_said(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    quiet = _ticket(double, lane="in_progress")
    summoned = _ticket(double, "@Aiko please look", lane="in_progress")
    answered = _ticket(double, lane="in_progress")
    double.comment(answered, "otota", "@aiko see this")
    mine = _ticket(double, lane="in_progress")
    double.comment(mine, MEMBER, "Done for now.")

    assert await _reasons(tickets) == [
        (quiet, "working_lane"),
        (summoned, "issue_mention"),
        (answered, "issue_comment"),
    ]


@pytest.mark.asyncio
async def test_a_mention_is_a_word_of_its_own(monkeypatch):
    _, _, tickets = services(monkeypatch)

    assert tickets.mentions("cc @AIKO.")
    assert tickets.mentions("@aiko, see this")
    assert not tickets.mentions("cc @aiko-bot")
    assert not tickets.mentions("cc @aiko.bot")
    assert not tickets.mentions("mail aiko@example.com @aikox")


@pytest.mark.asyncio
@pytest.mark.parametrize("closes", [True, False])
async def test_a_working_ticket_with_a_pull_request_is_the_patrols(monkeypatch, closes):
    double, code, tickets = services(monkeypatch)
    open_pull = _ticket(double, lane="in_progress")
    merged = _ticket(double, lane="in_progress")
    for branch, url in (("first", open_pull), ("second", merged)):
        double.branch(branch)
        await code.create_pull_request(
            REPO, branch, "", branch, "", draft=False, ticket_url=url, closes=closes
        )
    double.merge(next(u for u, p in double.pulls.items() if p["head"] == "second"))
    before = len(double.writes)

    assert await tickets.candidates() == []
    assert double.tickets[open_pull]["lane"] == "in_progress"
    # A merge finishes the ticket: Branvil closes the one it closes, and the
    # patrol moves one it only refers to.
    assert double.tickets[merged]["lane"] == "done"
    assert double.tickets[merged]["state"] == ("closed" if closes else "open")
    assert len(double.writes[before:]) == (0 if closes else 1)


@pytest.mark.asyncio
async def test_a_ticket_on_another_board_is_not_taken_off_it(monkeypatch):
    double, _, tickets = services(monkeypatch)
    url = _ticket(double)
    double.assign(url, board=OTHER_BOARD)
    before = len(double.writes)

    with pytest.raises(ServiceError, match=OTHER_BOARD):
        await tickets.add(url)
    with pytest.raises(ServiceError, match=OTHER_BOARD):
        await tickets.move(Task(url=url, title="", description=""), "done")

    assert double.writes[before:] == []
    assert double.tickets[url]["board"] == OTHER_BOARD


@pytest.mark.asyncio
async def test_a_ticket_off_the_board_is_not_moved_onto_it(monkeypatch):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(TRACKER, "Loose", "", [])["url"]

    with pytest.raises(ServiceError, match=BOARD):
        await tickets.move(Task(url=url, title="", description=""), "in_progress")

    assert double.tickets[url]["board"] is None


@pytest.mark.asyncio
async def test_a_board_outside_the_scope_is_not_written_to(monkeypatch):
    double, _, tickets = services(monkeypatch, board="other/board")
    url = _ticket(double)
    before = len(double.writes)

    with pytest.raises(ServiceError, match="refused"):
        await tickets.add(url)
    with pytest.raises(ServiceError, match="refused"):
        await tickets.move(Task(url=url, title="", description=""), "done")

    assert double.writes[before:] == []


@pytest.mark.asyncio
async def test_ready_tickets_come_first_then_the_oldest(monkeypatch):
    double, _, tickets = services(monkeypatch)
    working = _ticket(double, lane="in_progress")
    old, new = _ticket(double), _ticket(double)
    double.tickets[new]["created_at"] = "2026-01-01T00:00:00.000Z"

    assert [url for url, _ in await _reasons(tickets)] == [new, old, working]


@pytest.mark.asyncio
async def test_only_the_members_open_tickets_on_the_board_are_work(monkeypatch):
    double, _, tickets = services(monkeypatch)
    theirs = _ticket(double)
    double.assign(theirs, name="otota")
    closed = _ticket(double)
    double.tickets[closed]["state"] = "closed"
    new = _ticket(double, lane="new")

    assert await tickets.candidates() == []
    assert await tickets.refresh(Task(url=new, title="", description="")) is None
    # A ticket whose one assignee is someone else is not the member's work,
    # however it is read.
    assert await tickets.refresh(Task(url=theirs, title="", description="")) is None


@pytest.mark.asyncio
async def test_a_ticket_reads_as_its_one_assignee_or_none(monkeypatch):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(TRACKER, "Fix login", "", [])["url"]

    async def assignees() -> list[str]:
        return (await tickets.read("tickets", url)).items[0]["assignees"]

    assert await assignees() == []
    double.assign(url)
    assert await assignees() == [MEMBER]
    double.assign(url, name="otota")
    assert await assignees() == ["otota"]


@pytest.mark.asyncio
async def test_without_a_board_no_ticket_is_work_or_put_on_one(monkeypatch):
    double, _, tickets = services(monkeypatch, board="")
    url = _ticket(double)
    before = len(double.writes)

    assert await tickets.candidates() == []
    assert await tickets.add(url) is False
    assert await tickets.move(Task(url=url, title="", description=""), "done") is False
    now = datetime.now(UTC)
    assert await tickets.closed_since(now - timedelta(days=1), now) == []
    assert double.writes[before:] == []
    # A ticket on a board is on none the service is configured with.
    assert (await tickets.read("tickets", url)).items[0]["lane"] is None


@pytest.mark.asyncio
async def test_a_ticket_on_the_board_is_not_put_on_it_again(monkeypatch):
    double, _, tickets = services(monkeypatch)
    url = _ticket(double)
    before = len(double.writes)

    assert await tickets.add(url) is True

    assert double.writes[before:] == []
    assert double.tickets[url]["lane"] == "ready"


@pytest.mark.asyncio
async def test_a_status_comment_carries_the_status_and_addresses_the_author(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(TRACKER, "Fix login", "", [], author="otota")["url"]
    status = WorkflowStatus(reason="failed", person_id=MEMBER, run_id="r", subject=url)

    await tickets.comment(url, "It failed.", status=status)
    await tickets.comment(url, "Plain.")

    first, second = double.entries[url]["comments"]
    assert (first["body"], first["meta"]) == ("@otota\n\nIt failed.", status.payload())
    assert (second["body"], second["meta"]) == ("Plain.", None)
