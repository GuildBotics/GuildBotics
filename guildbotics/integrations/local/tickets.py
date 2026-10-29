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

import json
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from logging import Logger
from typing import IO, Any

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.local import items, store
from guildbotics.integrations.local.chat import mentions
from guildbotics.integrations.local.identity import member_identity
from guildbotics.integrations.repository_scope import check_path, in_scope, valid_path
from guildbotics.integrations.ticket_attachments import referred
from guildbotics.integrations.ticket_selection import (
    mention_pending,
    relevant_comments,
    select,
    select_mention,
)
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    Attachment,
    CommentWritten,
    ReactionAdded,
    ReadPage,
    TicketRelative,
    read_conditions,
)
from guildbotics.runtime.ticket_service import (
    ClosedItem,
    CreateLane,
    MoveLane,
    TicketCreated,
    TicketService,
    TicketUpdated,
    creation_lane,
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
        if resource == "tracker_tickets":
            if not valid_path(subject):
                raise ServiceNotFound(f"There is no tracker '{subject}'.")
            store.tracker(subject)
            terms = conditions["text"].casefold().split()
            found = [
                {
                    **items.target(ref, entry).model_dump(),
                    "key": ref.name,
                    "state": entry["state"],
                    "lane": entry["lane"],
                    "labels": entry["labels"],
                    "updated_at": entry["updated_at"],
                }
                for ref, entry in store.items("ticket", subject)
                if (
                    conditions["state"] == "all"
                    or entry["state"] == conditions["state"]
                )
                and (not conditions["label"] or conditions["label"] in entry["labels"])
                and all(
                    term in f"{entry['title']}\n{entry['body']}".casefold()
                    for term in terms
                )
            ]
            return items.page(resource, found, conditions, continuation)
        ref = store.locate(subject, "ticket")
        item = store.load(ref)
        if resource == "ticket_attachments":
            return items.page(
                resource,
                [attachment.model_dump() for attachment in _attachments(ref, item)],
                conditions,
                continuation,
            )
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
                if comment["author"] == member_identity(self.person).name
                else Message.USER,
                "created_at": comment["created_at"],
                "url": None,
            }
            for comment in item["comments"]
        ]
        return items.page(resource, comments, conditions, continuation)

    @asynccontextmanager
    async def attachment(
        self, url: str, attachment_id: str, limit: int
    ) -> AsyncIterator[tuple[Attachment, IO[bytes]]]:
        ref = store.locate(url, "ticket")
        found = referred(_attachments(ref, store.load(ref)), url, attachment_id, limit)
        with (store.attachments(ref.place) / found.id).open("rb") as content:
            yield found, content

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        ref = store.locate(url, "ticket")
        return items.comment(
            self.scope, ref, member_identity(self.person).name, body, status
        )

    async def create_ticket(
        self,
        tracker: str,
        title: str,
        body: str,
        labels: Sequence[str],
        *,
        add_to_board: bool = True,
        interactive: bool = False,
        assignee: str | None = None,
        lane: CreateLane | None = None,
        parent: str | None = None,
        blocked_by: Sequence[str] = (),
    ) -> TicketCreated:
        defined = _defined_labels(tracker, labels)
        initial_parent = self._related(parent) if parent else None
        blockers = list(dict.fromkeys(map(self._related, blocked_by)))
        ref, number = store.new_ref("ticket", tracker)
        item = {
            **items.new_item(number, title, body, member_identity(self.person).name),
            "labels": defined,
            "lane": creation_lane(lane, parent, blocked_by) if add_to_board else None,
            "interactive": interactive,
            "assignees": [assignee] if assignee else [],
            "parent": initial_parent,
            "blocked_by": blockers,
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
        return items.react(
            self.scope, ref, member_identity(self.person).name, target_id, reaction
        )

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
            task
            for ref, item in tickets
            if (task := await self._work(ref, item)) is not None
        ]

    async def refresh(self, task: Task) -> Task | None:
        assert task.url
        ref = store.locate(task.url, "ticket")
        return await self._work(ref, store.load(ref))

    async def can_receive_work(self, url: str) -> bool:
        ref = store.locate(url, "ticket")
        return await self._work(ref, store.load(ref), finish_merged=False) is not None

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

    def _interactive(self, item: dict[str, Any]) -> bool:
        """A ticket inherits its interactive parent's exclusion from patrol."""
        seen: set[str] = set()
        while True:
            if item["interactive"]:
                return True
            parent = item["parent"]
            if not parent or parent in seen:
                return False
            seen.add(parent)
            item = store.load(store.locate(parent, "ticket"))

    async def _work(
        self, ref: store.Ref, item: dict[str, Any], *, finish_merged: bool = True
    ) -> Task | None:
        me = member_identity(self.person).name
        lane = item["lane"]
        if (
            item["state"] != "open"
            or not in_scope(self.scope, ref.place)
            or self._interactive(item)
        ):
            return None
        assigned = me in item["assignees"]
        if assigned and (
            lane not in _LANES
            or (
                lane == Task.READY
                and any(
                    _relative(blocker).state == "open" for blocker in item["blocked_by"]
                )
            )
        ):
            return None
        comments = item["comments"]
        messages = [
            Message(
                content=comment["body"],
                author=comment["author"],
                author_type=Message.ASSISTANT
                if comment["author"] == me
                else Message.USER,
                timestamp=comment["created_at"],
            )
            for comment in comments
        ]
        task = items.task(
            ref,
            item,
            status=lane or Task.NEW,
            assignee=self.person.person_id if assigned else None,
            comments=messages,
        )

        def mentioned(text: str) -> bool:
            return me.casefold() in mentions(text.casefold())

        pending = mention_pending(
            mentioned(task.description),
            ((message, mentioned(message.content)) for message in messages),
        )
        if not assigned:
            return select_mention(task, pending=pending)

        async def no_pull() -> None:
            return None

        since = relevant_comments(
            (
                message,
                bool(mentions(message.content.casefold()))
                and not mentioned(message.content),
            )
            for message in messages
        )
        metadata = [
            comment
            for message, comment in zip(messages, comments, strict=True)
            if message in since
        ]

        return await select(
            task,
            since,
            latest_status=WorkflowStatus.from_payload(metadata[-1].get("status"))
            if metadata
            else None,
            pending=pending,
            linked_pull=no_pull,
            done=lambda: self.move(task, "done"),
            finish_merged=finish_merged,
        )


def _attachments(ref: store.Ref, item: dict[str, Any]) -> list[Attachment]:
    """The tracker's attachments the ticket's body or comments write the URL
    of, in the tracker's order."""
    texts = [
        ("body", item["body"]),
        *((comment["id"], comment["body"]) for comment in item["comments"]),
    ]
    folder = store.attachments(ref.place)
    found: list[Attachment] = []
    for meta in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        # A URL whose id goes on is another attachment's.
        written = re.compile(
            rf"{re.escape(store.attachment_url(ref.place, meta.stem))}(?![\w-])"
        )
        referenced_by = [where for where, text in texts if written.search(text)]
        if referenced_by:
            found.append(
                Attachment(
                    **json.loads(meta.read_text(encoding="utf-8")),
                    id=meta.stem,
                    size=(folder / meta.stem).stat().st_size,
                    referenced_by=referenced_by,
                )
            )
    return found


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
