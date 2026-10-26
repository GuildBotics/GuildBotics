"""Branvil as a member's tickets: the tickets of its repositories, taken from
the board of the workspace ``project.services.ticket_service.workspace`` names.

The selection's rules (:mod:`~guildbotics.integrations.ticket_selection`) are
applied here to the cards Branvil answers.

Ticket comments are mutable in Branvil's GUI: reads and candidate refreshes
use the current bodies and remaining comments, in their original creation
order. Edits keep the comment's author, creation time and workflow metadata.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from logging import Logger
from typing import Any

from guildbotics.entities import Person, Service, Task, Team
from guildbotics.entities.message import Message
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    BranvilApi,
    BranvilItem,
    segment,
    username,
)
from guildbotics.integrations.repository_scope import check_path, in_scope
from guildbotics.integrations.ticket_selection import (
    linked_pull_state,
    mention_pending,
    select,
    select_mention,
    since_assignment,
)
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    CommentWritten,
    ReactionAdded,
    ReadPage,
    Ticket,
    TicketRelative,
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
            repository=item.place,
            body=str(data.get("body") or ""),
            state=str(data["state"]),
            assignees=[str(data["assignee"]["name"])] if data["assignee"] else [],
            labels=list(data.get("labels") or []),
            lane=data["lane"] if self._on_board(data) else None,
            parent=data["parent"] and self._relative(data["parent"]),
            children=[self._relative(entry) for entry in data["children"]],
            blocked_by=[self._relative(entry) for entry in data["blocked_by"]],
            blocks=[self._relative(entry) for entry in data["blocks"]],
        )
        return ReadPage.of(resource, [ticket], target=self.target(item, data))

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        return await self.post_comment(self.item(url, "ticket"), body, status)

    async def create_ticket(
        self,
        tracker: str,
        title: str,
        body: str,
        labels: Sequence[str],
        *,
        add_to_board: bool = True,
    ) -> TicketCreated:
        """Open a ticket in the new lane of the board of its repository's
        workspace: Branvil keeps every ticket on it.

        Raises:
            ServiceUnsupported: If not ``add_to_board``.
        """
        if not add_to_board:
            raise ServiceUnsupported(t("integrations.branvil.api.every_on_board"))
        data = await self.call(
            "POST",
            f"/repos/{segment(self.repository(tracker))}/tickets",
            {"title": title, "body": body, "labels": list(labels), "lane": Task.NEW},
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
        parent: str | None = None,
        add_blocked_by: Sequence[str] = (),
        remove_blocked_by: Sequence[str] = (),
    ) -> TicketUpdated:
        item = self.item(url, "ticket")
        # A relation is written to both tickets: each is in the scope.
        for related in filter(None, (parent, *add_blocked_by, *remove_blocked_by)):
            check_path(
                self.workspace, self.item(related, "ticket").place, provider=PROVIDER
            )
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
                **({} if parent is None else {"parent": parent or None}),
                "add_labels": list(add_labels),
                "remove_labels": list(remove_labels),
                "add_blocked_by": list(add_blocked_by),
                "remove_blocked_by": list(remove_blocked_by),
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
        cards = await self.items(
            f"{self._board}/cards",
            lane=",".join(_LANES),
            assignee="me",
            state="open",
            comments="all",
        )
        mentioned = await self.items(
            f"/workspaces/{segment(self.workspace)}/tickets",
            mentions="me",
            state="open",
            comments="all",
        )
        cards = list({card["url"]: card for card in [*cards, *mentioned]}.values())
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
        """Move a ticket of the workspace's board to ``lane``.

        Raises:
            ServiceError: If the ticket's repository is outside the scope,
                its board not the workspace's.
        """
        assert task.url
        item = self.item(task.url, "ticket")
        check_path(self.workspace, item.place, provider=PROVIDER)
        await self.call("PUT", f"{item.api}/board", {"lane": lane})
        return True

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        closed = await self.items(
            f"{self._board}/closed",
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

    @property
    def _board(self) -> str:
        """The API path of the workspace's board."""
        return f"/workspaces/{segment(self.workspace)}/board"

    def _on_board(self, ticket: dict[str, Any]) -> bool:
        """Whether ``ticket`` is on the workspace's board: every ticket is on
        the board of its repository's workspace, and another workspace's
        board is not it."""
        return in_scope(self.workspace, str(ticket["repository"]))

    def _relative(self, ticket: dict[str, Any]) -> TicketRelative:
        return TicketRelative(
            **self.target(_ticket_item(ticket), ticket).model_dump(),
            state=str(ticket["state"]),
        )

    def _task(self, card: dict[str, Any]) -> Task | None:
        """An open card in scope, for assigned work or a mention response.

        Only assigned work is restricted to actionable lanes and blockers;
        a mention response can answer without starting the assigned work.
        """
        me = username(self.person).casefold()
        assigned = bool(card["assignee"]) and card["assignee"]["name"].casefold() == me
        if not (
            me
            and card["state"] == "open"
            and self._on_board(card)
            and (
                not assigned
                or (
                    card["lane"] in _LANES
                    and not (
                        card["lane"] == Task.READY
                        and any(
                            entry["state"] == "open" for entry in card["blocked_by"]
                        )
                    )
                )
            )
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
            assignee=self.person.person_id if assigned else None,
            comments=[self.message(comment) for comment in card["comments"]],
        )

    async def _select(self, task: Task, card: dict[str, Any]) -> Task | None:
        if task.assignee is None:
            return select_mention(
                task,
                pending=mention_pending(
                    self.mentions_member(card),
                    (
                        (message, self.mentions_member(comment))
                        for message, comment in zip(
                            task.comments, card["comments"], strict=True
                        )
                    ),
                ),
            )
        since = since_assignment(task.comments, card["assignee"]["assigned_at"])
        # Keep the source metadata and stored mention flags with the same
        # filtered messages; unknown timestamps need not form a suffix.
        comments = [
            (message, comment)
            for message, comment in zip(task.comments, card["comments"], strict=True)
            if message in since
        ]

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
            latest_status=WorkflowStatus.from_payload(comments[-1][1].get("meta"))
            if comments
            else None,
            pending=mention_pending(
                self.mentions_member(card),
                (
                    (message, self.mentions_member(comment))
                    for message, comment in comments
                ),
            ),
            linked_pull=linked_pull,
            done=lambda: self.move(task, "done"),
            addressed_elsewhere=bool(comments)
            and comments[-1][0].author_type != Message.ASSISTANT
            and bool(comments[-1][1]["mentions"])
            and not self.mentions_member(comments[-1][1]),
        )


def _ticket_item(ticket: dict[str, Any]) -> BranvilItem:
    return BranvilItem("ticket", str(ticket["repository"]), str(ticket["key"]))


def _timestamp(moment: datetime) -> str:
    """``moment`` as Branvil writes times: UTC, to the millisecond."""
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
