"""Which tickets of the workspace's Branvil board are a member's work, beyond what the
ticket service contract asks: the member's own word, a summons, a held
status, the time of the assignment, the pull request that takes a ticket
over, and the patrol's order."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.runtime.code_hosting_service import TicketLink
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    MEMBER,
    REPO,
    SCOPE,
    THEIR_REPO,
    BranvilDouble,
    services,
)


def _status(reason: str, **fields: str) -> dict[str, str]:
    return WorkflowStatus.model_validate(
        {"reason": reason, "person_id": MEMBER, "run_id": "r", **fields}
    ).payload()


def _ticket(double: BranvilDouble, body: str = "", lane: str = "ready") -> str:
    url = double._ticket(REPO, "Fix login", body, [])["url"]
    double.assign(url, lane)
    return url


async def _reasons(tickets) -> list[tuple[str | None, str | None]]:
    return [(task.url, task.trigger_reason) for task in await tickets.candidates()]


@pytest.mark.asyncio
async def test_comment_edits_and_deletions_are_read_again_before_dispatch(monkeypatch):
    double, _, tickets = services(monkeypatch)
    url = _ticket(double, lane="in_progress")
    original = double.comment(url, "otota", "Original")
    [task] = await tickets.candidates()
    assert [c.content for c in task.comments] == ["Original"]

    # The GUI's edit replaces only the body; it is not a new comment.
    original["body"] = "Edited"
    [comment] = (await tickets.read("ticket_comments", url)).items
    assert (comment["id"], comment["body"], comment["created_at"]) == (
        original["id"],
        "Edited",
        original["created_at"],
    )
    refreshed = await tickets.refresh(task)
    assert refreshed is not None
    assert [c.content for c in refreshed.comments] == ["Edited"]

    # A deleted member status stops holding the ticket, including on refresh.
    double.comment(url, MEMBER, "Failed", **_status("failed"))
    assert await tickets.refresh(task) is None
    double.entries[url]["comments"].pop()
    refreshed = await tickets.refresh(task)
    assert refreshed is not None
    assert [c.content for c in refreshed.comments] == ["Edited"]
    double.entries[url]["comments"].clear()
    assert (await tickets.read("ticket_comments", url)).items == []
    refreshed = await tickets.refresh(task)
    assert refreshed is not None
    assert refreshed.comments == []
    [candidate] = await tickets.candidates()
    assert candidate.comments == []


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
    assert (task.label, task.provider, task.assignee) == ("#1", "branvil", MEMBER)
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
    url = double._ticket(REPO, "Fix login", "", [])["url"]
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
            REPO, branch, "", branch, "", draft=False, tickets=[TicketLink(url, closes)]
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
@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("mention", [False, True])
async def test_ticket_conversations_are_answered_before_a_linked_pr_takes_over(
    monkeypatch, merged, mention
):
    double, code, tickets = services(monkeypatch)
    url = _ticket(double, "@aiko please" if mention else "", lane="in_progress")
    if not mention:
        double.comment(url, MEMBER, "Should I add the check?")
        double.comment(url, "otota", "Yes, please.")
    double.branch("feature")
    pull = await code.create_pull_request(
        REPO, "feature", "", "Work", "", draft=False, tickets=[TicketLink(url, False)]
    )
    if merged:
        double.merge(pull.target.url)

    [task] = await tickets.candidates()
    refreshed = await tickets.refresh(task)

    assert refreshed is not None
    assert refreshed.trigger_reason == ("issue_mention" if mention else "issue_comment")
    assert double.tickets[url]["lane"] == "in_progress"
    await tickets.comment(url, "Added the check.")
    assert await tickets.refresh(task) is None
    assert double.tickets[url]["lane"] == ("done" if merged else "in_progress")


@pytest.mark.asyncio
async def test_a_ticket_of_another_workspace_is_neither_work_nor_written_to(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(THEIR_REPO, "Elsewhere", "", [])["url"]
    double.assign(url)
    before = len(double.writes)

    # It is on its own workspace's board, which is not the member's.
    assert await tickets.candidates() == []
    assert (await tickets.read("tickets", url)).items[0]["lane"] is None
    with pytest.raises(ServiceError, match="refused"):
        await tickets.move(Task(url=url, title="", description=""), "done")

    assert double.writes[before:] == []
    assert double.tickets[url]["lane"] == "ready"


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
    url = double._ticket(REPO, "Fix login", "", [])["url"]

    async def assignees() -> list[str]:
        return (await tickets.read("tickets", url)).items[0]["assignees"]

    assert await assignees() == []
    double.assign(url)
    assert await assignees() == [MEMBER]
    double.assign(url, name="otota")
    assert await assignees() == ["otota"]


@pytest.mark.asyncio
async def test_a_ticket_is_created_in_the_new_lane_and_never_off_the_board(
    monkeypatch,
):
    double = BranvilDouble()
    sent: list[dict] = []
    respond = double.respond

    def recorded(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tickets"):
            sent.append(json.loads(request.content))
        return respond(request)

    monkeypatch.setattr(double, "respond", recorded)
    _, _, tickets = services(monkeypatch, double)

    with pytest.raises(
        ServiceUnsupported,
        match=re.escape(t("integrations.branvil.api.every_on_board")),
    ):
        await tickets.create_ticket(REPO, "Loose", "", [], add_to_board=False)
    created = await tickets.create_ticket(REPO, "Fix login", "", [])

    assert [body["lane"] for body in sent] == ["new"]
    assert double.tickets[created.target.url]["lane"] == "new"


@pytest.mark.asyncio
async def test_a_status_comment_carries_the_status_and_addresses_the_author(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(REPO, "Fix login", "", [], author="otota")["url"]
    status = WorkflowStatus(reason="failed", person_id=MEMBER, run_id="r", subject=url)

    await tickets.comment(url, "It failed.", status=status)
    await tickets.comment(url, "Plain.")

    first, second = double.entries[url]["comments"]
    assert (first["body"], first["meta"]) == ("@otota\n\nIt failed.", status.payload())
    assert (second["body"], second["meta"]) == ("Plain.", None)


@pytest.mark.asyncio
async def test_a_repository_numbers_its_tickets_and_defines_their_labels(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    created = await tickets.create_ticket(REPO, "Fix login", "", ["bug"])

    # Its own tickets count from 1, whatever another repository holds.
    assert double.tickets[double.their_ticket]["key"] == "1"
    assert (created.target.url, created.target.label) == (
        f"{BASE}/{REPO}/-/tickets/1",
        "#1",
    )
    [ticket] = (await tickets.read("tickets", created.target.url)).items
    assert (ticket["key"], ticket["tracker"], ticket["repository"]) == (
        "1",
        REPO,
        REPO,
    )
    # A label the repository does not define is refused.
    with pytest.raises(ServiceError, match="repository"):
        await tickets.create_ticket(REPO, "Docs", "", ["docs"])
    with pytest.raises(ServiceError, match="repository"):
        await tickets.update_ticket(
            created.target.url,
            body=None,
            title=None,
            add_labels=["docs"],
            remove_labels=[],
            state=None,
            state_reason=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/{REPO}/-/tickets/01",
        f"{BASE}/{REPO}/-/tickets/0",
        f"{BASE}/{REPO}/-/tickets/DEMO-1",
        f"{BASE}/{REPO}/-/tickets/1?x=1",
        f"{BASE}/{REPO}/-/pulls/1",
        f"{BASE}/demo/-/tickets/1",
    ],
)
async def test_an_address_of_no_ticket_is_not_found(monkeypatch, url):
    double, _, tickets = services(monkeypatch)
    double._ticket(REPO, "Fix login", "", [])
    before = len(double.writes)

    # Refused by its address, before any request.
    with pytest.raises(ServiceNotFound) as refused:
        await tickets.comment(url, "Hi")

    assert str(refused.value) == t("integrations.branvil.api.not_ticket", url=url)
    assert double.writes[before:] == []


@pytest.mark.asyncio
async def test_the_boards_closed_items_are_of_its_workspaces_repositories(
    monkeypatch,
):
    double, code, tickets = services(monkeypatch)
    double.branch("feature")
    pull = (
        await code.create_pull_request(
            REPO, "feature", "", "Fix", "", draft=False, tickets=[]
        )
    ).target.url
    double.merge(pull)
    now = datetime.now(UTC)
    window = (now - timedelta(days=3650), now + timedelta(days=3650))

    # The workspace's pull requests are listed with no card on the board.
    assert [item.url for item in await tickets.closed_since(*window)] == [pull]

    # So are its closed tickets; another workspace's are not.
    closed = double._ticket(REPO, "Closed", "", [])["url"]
    theirs = double._ticket(THEIR_REPO, "Elsewhere", "", [])["url"]
    for url in (closed, theirs):
        double.tickets[url].update(state="closed", closed_at=double.now(), lane="done")
    assert {item.url for item in await tickets.closed_since(*window)} == {pull, closed}


async def _relate(tickets, url: str, **relations) -> None:
    await tickets.update_ticket(
        url,
        body=None,
        title=None,
        add_labels=[],
        remove_labels=[],
        state=None,
        state_reason=None,
        **relations,
    )


@pytest.mark.asyncio
async def test_a_blocked_ticket_waits_in_the_ready_lane_but_not_once_started(
    monkeypatch,
):
    double, _, tickets = services(monkeypatch)
    ready, working = _ticket(double), _ticket(double, lane="in_progress")
    blocker = double._ticket(REPO, "Blocker", "", [])["url"]
    for url in (ready, working):
        await _relate(tickets, url, add_blocked_by=[blocker])

    assert await _reasons(tickets) == [(working, "working_lane")]
    assert await tickets.refresh(Task(url=ready, title="", description="")) is None
    # Branvil itself refuses to start it.
    with pytest.raises(ServiceError, match="blocked"):
        await tickets.move(Task(url=ready, title="", description=""), "in_progress")


@pytest.mark.asyncio
async def test_an_update_that_names_no_parent_keeps_it(monkeypatch):
    double, _, tickets = services(monkeypatch)
    parent, child = [double._ticket(REPO, t, "", [])["url"] for t in ("P", "C")]
    await _relate(tickets, child, parent=parent)

    await tickets.update_ticket(
        child,
        body=None,
        title="Renamed",
        add_labels=[],
        remove_labels=[],
        state=None,
        state_reason=None,
    )

    [read] = (await tickets.read("tickets", child)).items
    assert (read["title"], read["parent"]["url"], read["parent"]["label"]) == (
        "Renamed",
        parent,
        "#1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["parent", "add_blocked_by", "remove_blocked_by"])
async def test_a_relation_to_another_workspace_is_refused_before_any_request(
    monkeypatch, relation
):
    double, _, tickets = services(monkeypatch)
    url = double._ticket(REPO, "Mine", "", [])["url"]
    theirs = double.their_ticket
    before = len(double.writes)

    # The scope's refusal, before anything is sent.
    with pytest.raises(ServiceError, match=f"Writes are limited to '{SCOPE}'"):
        await _relate(
            tickets, url, **{relation: theirs if relation == "parent" else [theirs]}
        )

    assert double.writes[before:] == []
