"""What the local tickets keep of their relations beyond the port's contract."""

import pytest

from guildbotics.entities import Task
from guildbotics.integrations.local.tickets import LocalTicketService
from guildbotics.runtime.port import ServiceNotFound
from tests.guildbotics.local_code_host import TRACKER, item, local_member, ticket


def _tickets() -> LocalTicketService:
    tickets = local_member().tickets
    assert isinstance(tickets, LocalTicketService)
    return tickets


async def _block(tickets: LocalTicketService, url: str, *blockers: str) -> None:
    await tickets.update_ticket(
        url,
        body=None,
        title=None,
        add_labels=(),
        remove_labels=(),
        state=None,
        state_reason=None,
        add_blocked_by=blockers,
    )


@pytest.mark.asyncio
async def test_a_ticket_already_being_worked_on_is_taken_up_while_blocked():
    blocker = ticket(1)
    url = ticket(2, lane=Task.IN_PROGRESS, assignees=["aiko"])
    tickets = _tickets()

    await _block(tickets, url, blocker)

    assert [task.url for task in await tickets.candidates()] == [url]


@pytest.mark.asyncio
async def test_a_relation_to_no_ticket_is_refused_and_nothing_is_written():
    url = ticket(1)
    tickets = _tickets()

    with pytest.raises(ServiceNotFound):
        await _block(tickets, url, f"local://tickets/{TRACKER}/T-9")

    assert item(url)["blocked_by"] == []
