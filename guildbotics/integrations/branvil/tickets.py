"""Branvil as a member's tickets: the tickets of its trackers, and the board
named by ``project.services.ticket_service.board`` they are taken from.

Without a board no ticket is the member's work, and none is put on one. The
selection's rules (:mod:`~guildbotics.integrations.ticket_selection`) are
applied here to the cards Branvil answers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from logging import Logger
from typing import Any

from guildbotics.entities import Person, Service, Task, Team
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    BranvilApi,
    BranvilItem,
    segment,
    username,
)
from guildbotics.integrations.repository_scope import check_path
from guildbotics.integrations.ticket_selection import (
    linked_pull_state,
    mention_pending,
    select,
    since_assignment,
)
from guildbotics.runtime.port import SemanticReaction, ServiceError, WorkflowStatus
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    CommentWritten,
    ReactionAdded,
    ReadPage,
    Ticket,
    read_conditions,
)
from guildbotics.runtime.ticket_service import (
    ClosedItem,
    MoveLane,
    TicketCreated,
    TicketService,
    TicketUpdated,
)
from guildbotics.utils.i18n_tool import t

#: The lanes whose tickets are a member's work.
_LANES = (Task.READY, Task.IN_PROGRESS)


class BranvilTicketService(BranvilApi, TicketService):
    def __init__(self, logger: Logger, person: Person, team: Team):
        TicketService.__init__(self, logger, person, team)
        BranvilApi.__init__(self, person, team, Service.TICKET_SERVICE)
        config = team.project.get_service_config(Service.TICKET_SERVICE)
        #: The board the member's tickets are taken from; empty for none.
        self.board = str(config.get("board") or "")

    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        conditions = read_conditions(resource, parameters, continuation)
        if resource not in TICKET_RESOURCES:
            raise ServiceError(t("integrations.resources.resource"))
        item = self.item(subject, "ticket")
        if resource == "ticket_comments":
            return await self.page(
                resource,
                f"{item.api}/comments",
                conditions,
                continuation,
                self.comment_of,
            )
        data = await self.get(item.api)
        ticket = Ticket(
            **self.target(item, data).model_dump(),
            key=item.name,
            tracker=item.place,
            repository=data.get("repository"),
            body=str(data.get("body") or ""),
            state=str(data["state"]),
            assignees=[str(assignee["name"]) for assignee in data["assignees"]],
            labels=list(data.get("labels") or []),
            lane=data["lane"] if self._on_board(data) else None,
        )
        return ReadPage.of(resource, [ticket], target=self.target(item, data))

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        return await self.post_comment(self.item(url, "ticket"), body, status)

    async def create_ticket(
        self, tracker: str, title: str, body: str, labels: Sequence[str]
    ) -> TicketCreated:
        data = await self.call(
            "POST",
            f"/trackers/{segment(self.repository(tracker))}/tickets",
            {"title": title, "body": body, "labels": list(labels)},
        )
        return TicketCreated(
            labels=list(data["labels"]), target=self.target(_ticket_item(data), data)
        )

    async def update_ticket(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        state: str | None,
        state_reason: str | None,
    ) -> TicketUpdated:
        item = self.item(url, "ticket")
        changes = {
            "body": body,
            "title": title,
            "state": state,
            "state_reason": None if state is None else state_reason,
        }
        answer = await self.call(
            "PATCH",
            item.api,
            {
                **{key: value for key, value in changes.items() if value is not None},
                "add_labels": list(add_labels),
                "remove_labels": list(remove_labels),
            },
        )
        data = answer["ticket"]
        return TicketUpdated(
            body=str(data.get("body") or ""),
            state=str(data["state"]),
            state_changed=bool(answer["state_changed"]),
            labels=list(data["labels"]),
            target=self.target(item, data),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        return await self.react(self.item(url, "ticket"), target_id, reaction)

    # -- the board ----------------------------------------------------------

    async def candidates(self) -> list[Task]:
        """Every actionable ticket of the board, ready lane first, oldest
        first in each lane."""
        if not self.board:
            return []
        cards = await self.items(
            f"/boards/{segment(self.board)}/cards",
            lane=",".join(_LANES),
            assignee="me",
            state="open",
            comments="all",
        )
        found = [(task, card) for card in cards if (task := self._task(card))]
        ordered = sorted(found, key=lambda pair: pair[0])
        candidates = []
        for task, card in [
            *(pair for pair in ordered if pair[0].status == Task.READY),
            *(pair for pair in ordered if pair[0].status != Task.READY),
        ]:
            if (selected := await self._select(task, card)) is not None:
                candidates.append(selected)
        return candidates

    async def refresh(self, task: Task) -> Task | None:
        assert task.url
        item = self.item(task.url, "ticket")
        card = {
            **await self.get(item.api),
            "comments": await self.items(f"{item.api}/comments"),
            "pull_requests": await self.items(f"{item.api}/pulls"),
        }
        found = self._task(card)
        return None if found is None else await self._select(found, card)

    async def move(self, task: Task, lane: MoveLane) -> bool:
        """Move a ticket of the configured board to ``lane``.

        Raises:
            ServiceError: If the ticket is not on that board.
        """
        assert task.url
        item = self.item(task.url, "ticket")
        if not self.board:
            return False
        if not self._on_board(await self._placed(item)):
            raise ServiceError(
                t(
                    "integrations.branvil.api.off_board",
                    url=self.url(item),
                    board=self.board,
                )
            )
        await self.call("PUT", f"{item.api}/board", {"board": self.board, "lane": lane})
        return True

    async def add(self, ticket_url: str) -> bool:
        item = self.item(ticket_url, "ticket")
        if not self.board:
            return False
        if not self._on_board(await self._placed(item)):
            await self.call(
                "PUT", f"{item.api}/board", {"board": self.board, "lane": Task.NEW}
            )
        return True

    async def _placed(self, item: BranvilItem) -> dict[str, Any]:
        """The ticket ``item``, to be placed on the configured board: both its
        tracker and the board are in the scope -- checked before anything,
        as a ticket already on the board is not written to -- and it is on
        no other board, which it is not taken off silently.

        Raises:
            ServiceError: If either is outside the scope, or the ticket is on
                another board.
        """
        check_path(self.owner, item.place, provider=PROVIDER)
        check_path(self.owner, self.board, provider=PROVIDER)
        ticket = await self.get(item.api)
        other = str(ticket.get("board") or "")
        if other and not self._on_board(ticket):
            raise ServiceError(
                t(
                    "integrations.branvil.api.other_board",
                    url=self.url(item),
                    board=other,
                )
            )
        return ticket

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        if not self.board:
            return []
        closed = await self.items(
            f"/boards/{segment(self.board)}/closed",
            since=_timestamp(start),
            until=_timestamp(end),
        )
        return [
            ClosedItem(
                kind=item["kind"],
                url=str(item["url"]),
                title=str(item.get("title") or ""),
                label=str(item["label"]),
                closed_at=str(item["closed_at"]),
                merged=bool(item.get("merged")),
            )
            for item in closed
        ]

    async def check_board(self) -> None:
        """Confirm that the configured board is there for the member."""
        await self.get(f"/boards/{segment(self.board)}")

    def _on_board(self, ticket: dict[str, Any]) -> bool:
        return bool(self.board) and str(ticket.get("board") or "").casefold() == (
            self.board.casefold()
        )

    def _task(self, card: dict[str, Any]) -> Task | None:
        """The card as a task of the member, if it is one: open, in a lane of
        the member's work on the board, and assigned to the member."""
        me = username(self.person).casefold()
        if not (
            me
            and card["state"] == "open"
            and self._on_board(card)
            and card["lane"] in _LANES
            and any(a["name"].casefold() == me for a in card["assignees"])
        ):
            return None
        item = _ticket_item(card)
        url = self.url(item)
        return Task(
            provider=PROVIDER,
            label=item.label,
            id=url,
            url=url,
            title=str(card.get("title") or ""),
            description=str(card.get("body") or ""),
            status=card["lane"],
            created_at=card.get("created_at"),
            assignee=self.person.person_id,
            comments=[self.message(comment) for comment in card["comments"]],
        )

    async def _select(self, task: Task, card: dict[str, Any]) -> Task | None:
        me = username(self.person).casefold()
        assigned_at = max(
            str(a.get("assigned_at") or "")
            for a in card["assignees"]
            if a["name"].casefold() == me
        )
        since = since_assignment(task.comments, assigned_at)

        async def linked_pull() -> str | None:
            return linked_pull_state(
                (
                    "merged" if pull.get("merged") else str(pull["state"]),
                    str(pull.get("updated_at") or ""),
                )
                for pull in card["pull_requests"]
            )

        return await select(
            task,
            since,
            # What was said since the assignment ends the comments.
            latest_status=WorkflowStatus.from_payload(card["comments"][-1].get("meta"))
            if since
            else None,
            pending=mention_pending(task.description, since, self.mentions),
            linked_pull=linked_pull,
            done=lambda: self.move(task, "done"),
        )


def _ticket_item(ticket: dict[str, Any]) -> BranvilItem:
    return BranvilItem("ticket", str(ticket["tracker"]), str(ticket["key"]))


def _timestamp(moment: datetime) -> str:
    """``moment`` as Branvil writes times: UTC, to the millisecond."""
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
