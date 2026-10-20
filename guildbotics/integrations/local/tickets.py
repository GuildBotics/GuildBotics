"""The local tickets: files of the workspace kept in trackers (:mod:`.store`),
and the board that is their lanes and whose they are (``assignees``).

A ticket in the ready lane is the member's to take unless the member spoke
last; one in the working lane is taken up again when someone else has spoken
since, or when nobody has said anything yet. A ticket in the ready lane
is not taken while a ticket it is blocked by is open.

A ticket keeps the URLs of its ``parent`` and of the tickets it is
``blocked_by``; its children and the tickets it blocks are the tickets that
name it so.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from logging import Logger
from typing import Any

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.local import items, store
from guildbotics.integrations.repository_scope import check_path
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    CommentWritten,
    ReactionAdded,
    ReadPage,
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

_LANES = (Task.READY, Task.IN_PROGRESS)


class LocalTicketService(TicketService):
    def __init__(self, logger: Logger, person: Person, team: Team):
        super().__init__(logger, person, team)
        config = team.project.get_service_config(Service.TICKET_SERVICE)
        #: The namespace the member writes in.
        self.scope = str(config.get("owner") or "")

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
        ref = store.locate(subject, "ticket")
        item = store.load(ref)
        if resource == "tickets":
            others = store.items("ticket")
            parent = item["parent"]
            ticket = {
                **items.target(ref, item).model_dump(),
                "key": ref.name,
                "tracker": ref.place,
                "repository": store.tracker(ref.place).get("repository"),
                "body": item["body"],
                "state": item["state"],
                "assignees": item["assignees"],
                "labels": item["labels"],
                "lane": item["lane"],
                "parent": _relative(parent) if parent else None,
                "children": [
                    _relative(other.url)
                    for other, each in others
                    if each["parent"] == ref.url
                ],
                "blocked_by": [_relative(url) for url in item["blocked_by"]],
                "blocks": [
                    _relative(other.url)
                    for other, each in others
                    if ref.url in each["blocked_by"]
                ],
            }
            return ReadPage.of(resource, [ticket], target=items.target(ref, item))
        comments = [
            {
                "id": comment["id"],
                "body": comment["body"],
                "author": comment["author"],
                "author_type": Message.ASSISTANT
                if comment["author"] == self.person.person_id
                else Message.USER,
                "created_at": comment["created_at"],
                "url": None,
            }
            for comment in item["comments"]
        ]
        return items.page(resource, comments, conditions, continuation)

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        ref = store.locate(url, "ticket")
        return items.comment(self.scope, ref, self.person.person_id, body, status)

    async def create_ticket(
        self,
        tracker: str,
        title: str,
        body: str,
        labels: Sequence[str],
        *,
        add_to_board: bool = True,
    ) -> TicketCreated:
        defined = _defined_labels(tracker, labels)
        ref, number = store.new_ref("ticket", tracker)
        item = {
            **items.new_item(number, title, body, self.person.person_id),
            "labels": defined,
            "lane": Task.NEW if add_to_board else None,
            "parent": None,
            "blocked_by": [],
        }
        store.save(self.scope, ref, item)
        return TicketCreated(labels=defined, target=items.target(ref, item))

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
        ref = store.locate(url, "ticket")
        item = store.load(ref)
        additions = _defined_labels(ref.place, add_labels)
        if parent is not None:
            # The parent it leaves changes too.
            if item["parent"]:
                self._related(item["parent"])
            item["parent"] = self._related(parent) if parent else None
        removed = [self._related(blocker) for blocker in remove_blocked_by]
        blockers = [blocker for blocker in item["blocked_by"] if blocker not in removed]
        item["blocked_by"] = blockers + [
            blocker
            for blocker in map(self._related, add_blocked_by)
            if blocker not in blockers
        ]
        state_changed = state is not None and item["state"] != state
        for key, value in (("body", body), ("title", title), ("state", state)):
            if value is not None:
                item[key] = value
        if state_changed:
            item["closed_at"] = store.now() if state == "closed" else None
        labels = [label for label in item["labels"] if label not in remove_labels]
        item["labels"] = labels + [label for label in additions if label not in labels]
        store.save(self.scope, ref, item)
        return TicketUpdated(
            body=item["body"],
            state=item["state"],
            state_changed=state_changed,
            labels=item["labels"],
            target=items.target(ref, item),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        ref = store.locate(url, "ticket")
        return items.react(self.scope, ref, self.person.person_id, target_id, reaction)

    # -- the board ----------------------------------------------------------

    async def candidates(self) -> list[Task]:
        tickets = sorted(
            store.items("ticket"),
            key=lambda ticket: (
                _LANES.index(ticket[1]["lane"])
                if ticket[1]["lane"] in _LANES
                else len(_LANES),
                ticket[1]["created_at"],
            ),
        )
        return [
            task for ref, item in tickets if (task := self._work(ref, item)) is not None
        ]

    async def refresh(self, task: Task) -> Task | None:
        assert task.url
        ref = store.locate(task.url, "ticket")
        return self._work(ref, store.load(ref))

    async def move(self, task: Task, lane: MoveLane) -> bool:
        assert task.url
        ref = store.locate(task.url, "ticket")
        item = store.load(ref)
        item["lane"] = lane
        store.save(self.scope, ref, item)
        return True

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        return [
            ClosedItem(
                kind=ref.kind,
                url=ref.url,
                title=item["title"],
                label=ref.label,
                closed_at=item["closed_at"],
                merged=bool(item.get("merged")),
            )
            for ref, item in [*store.items("ticket"), *store.items("pull_request")]
            if item["closed_at"]
            and start <= datetime.fromisoformat(item["closed_at"]) <= end
        ]

    async def aclose(self) -> None:
        """The files are opened per operation."""

    def _related(self, url: str) -> str:
        """The URL of the ticket ``url`` names, one whose relations the member
        may change: a ticket related to, or the parent a ticket leaves.

        Raises:
            ServiceNotFound: If there is no such ticket.
            ServiceError: If its tracker is outside the member's scope.
        """
        ref = store.locate(url, "ticket")
        store.load(ref)
        check_path(self.scope, ref.place, provider="local")
        return ref.url

    def _work(self, ref: store.Ref, item: dict[str, Any]) -> Task | None:
        me = self.person.person_id
        lane = item["lane"]
        if lane not in _LANES or me not in item["assignees"]:
            return None
        if lane == Task.READY and any(
            _relative(blocker).state == "open" for blocker in item["blocked_by"]
        ):
            return None
        comments = item["comments"]
        mine = bool(comments) and comments[-1]["author"] == me
        if lane == Task.READY:
            reason = None if mine else "ready_lane"
        elif comments:
            reason = None if mine else "issue_comment"
        else:
            reason = "working_lane"
        if reason is None:
            return None
        return items.task(ref, item, status=lane, assignee=me, trigger_reason=reason)


def _relative(url: str) -> TicketRelative:
    """The ticket ``url`` names, as a ticket related to it reads it."""
    ref = store.locate(url, "ticket")
    item = store.load(ref)
    return TicketRelative(**items.target(ref, item).model_dump(), state=item["state"])


def _defined_labels(tracker: str, labels: Sequence[str]) -> list[str]:
    """``labels``, each defined by the tracker.

    Raises:
        ServiceNotFound: If there is no such tracker.
        ServiceError: If one is not defined there.
    """
    defined = store.tracker(tracker).get("labels", [])
    undefined = [label for label in labels if label not in defined]
    if undefined:
        raise ServiceError(
            f"Label '{undefined[0]}' is not defined in {tracker}. "
            f"Defined labels: {', '.join(defined) or '(none)'}. "
            "Ask a human to add the label instead of introducing a new one."
        )
    return list(dict.fromkeys(labels))
