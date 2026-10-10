"""Every board of the factory keeps the same contract.

The same steps run against each provider: a ticket assigned to the member in
the ready lane is a candidate, moves to the working lane, and is re-read as
work still to do there; an issue is put on the board; the board's closed work
is listed for the activity history. Moving or adding another owner's issue is
refused before it is written, as every write outside the configured owner is.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from guildbotics.entities.task import Task
from guildbotics.integrations.factory import PROVIDERS
from tests.guildbotics.integrations.contracts.providers import (
    OWNER,
    REPO,
    Harness,
    harness,
)


@pytest_asyncio.fixture(
    params=sorted(name for name, p in PROVIDERS.items() if p.ticket_manager)
)
async def board(request, tmp_path, monkeypatch):
    provided = harness(request.param, tmp_path, monkeypatch)
    yield provided
    await provided.aclose()


@pytest.mark.asyncio
async def test_an_assigned_ready_ticket_is_taken_and_moved_to_the_working_lane(
    board: Harness,
):
    issue = await board.code.create_issue(REPO, "Fix login", "It fails.", [])
    await board.code.create_issue(REPO, "Not mine", "", [])
    board.assign(issue.issue_url)

    [task] = await board.board.get_task_candidates()

    assert (task.url, task.status, task.trigger_reason) == (
        issue.issue_url,
        Task.READY,
        "ready_lane",
    )
    assert (task.number, task.repository, task.title) == (1, "demo", "Fix login")
    assert await board.board.get_ticket_url(task, markdown=False) == issue.issue_url
    assert await board.board.move_ticket(task, Task.IN_PROGRESS) is True
    refreshed = await board.board.refresh_task(task)
    assert refreshed is not None
    assert (refreshed.status, refreshed.trigger_reason) == (
        Task.IN_PROGRESS,
        "working_lane",
    )

    # The member's own word is an answer: the ticket waits for someone else.
    await board.code.comment(issue.issue_url, "On it.", kind="issue")
    assert await board.board.refresh_task(task) is None


@pytest.mark.asyncio
async def test_an_issue_is_put_on_the_board_without_being_assigned(board: Harness):
    issue = await board.code.create_issue(REPO, "Later", "", [])

    assert await board.board.add_ticket(issue.issue_url)
    assert await board.board.get_task_candidates() == []


@pytest.mark.asyncio
async def test_the_boards_closed_work_is_listed(board: Harness):
    closed = await board.code.create_issue(REPO, "Done", "", [])
    still_open = await board.code.create_issue(REPO, "Open", "", [])
    for issue in (closed, still_open):
        board.assign(issue.issue_url)
    await board.code.update_issue(
        closed.issue_url,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(),
        state="closed",
        state_reason="completed",
    )

    items = await board.board.closed_since(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )

    assert [
        (item.kind, item.repo, item.number, item.url, item.merged) for item in items
    ] == [("issue", REPO, closed.issue_number, closed.issue_url, False)]
    assert (
        await board.board.closed_since(
            datetime(2000, 1, 1, tzinfo=UTC), datetime(2001, 1, 1, tzinfo=UTC)
        )
        == []
    )


@pytest.mark.asyncio
async def test_the_board_does_not_write_another_owners_issue(board: Harness):
    """The board keeps another owner's issue in that owner's repository (local)
    or records itself in its timeline (GitHub), so moving or adding it writes
    outside the configured owner."""
    before = len(board.writes)
    refused = f"Writes are limited to repositories of '{OWNER}'"

    with pytest.raises(RuntimeError, match=refused):
        await board.board.move_ticket(board.theirs, Task.IN_PROGRESS)
    with pytest.raises(RuntimeError, match=refused):
        await board.board.add_ticket(board.theirs.url or "")

    assert board.writes[before:] == []
