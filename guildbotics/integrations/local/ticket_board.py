"""The local board: the lane of each issue of the local code host
(:mod:`.store`), and whose it is (its ``assignees``).

A ticket in the ready lane is the member's to take unless the member spoke
last; one in the working lane is taken up again when someone else has spoken
since, or when nobody has said anything yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from guildbotics.entities import Task
from guildbotics.integrations.local import store
from guildbotics.runtime.code_hosting_service import ClosedItem
from guildbotics.runtime.ticket_manager import TicketManager

_LANES = (Task.READY, Task.IN_PROGRESS)


class LocalTicketManager(TicketManager):
    async def get_task_candidates(self) -> list[Task]:
        tickets = sorted(
            (
                (owner, repo, item)
                for owner, repo in store.repositories()
                for item in store.items(owner, repo)
                if item["kind"] == "issue"
            ),
            key=lambda ticket: (
                _LANES.index(ticket[2]["lane"])
                if ticket[2].get("lane") in _LANES
                else len(_LANES),
                ticket[2]["created_at"],
            ),
        )
        return [
            task
            for owner, repo, item in tickets
            if (task := self._work(owner, repo, item)) is not None
        ]

    async def refresh_task(self, task: Task) -> Task | None:
        assert task.url
        ref = store.locate(task.url, "issue")
        return self._work(
            ref.owner, ref.repo, store.load(ref.owner, ref.repo, ref.number)
        )

    async def move_ticket(self, task: Task, new_status: str) -> bool:
        assert task.url
        ref = store.locate(task.url, "issue")
        item = store.load(ref.owner, ref.repo, ref.number)
        item["lane"] = new_status
        store.save(self.team.project, ref.owner, ref.repo, item)
        return True

    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        assert task.url
        return f"[{task.title}]({task.url})" if markdown else task.url

    async def add_ticket(self, issue_url: str) -> str | None:
        ref = store.locate(issue_url, "issue")
        item = store.load(ref.owner, ref.repo, ref.number)
        item["lane"] = item.get("lane") or Task.NEW
        store.save(self.team.project, ref.owner, ref.repo, item)
        return ref.url

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        return [
            ClosedItem(
                kind=item["kind"],
                repo=f"{owner}/{repo}",
                number=item["number"],
                title=item["title"],
                url=store.url(owner, repo, item["kind"], item["number"]),
                closed_at=item["closed_at"],
                merged=bool(item.get("merged")),
            )
            for owner, repo in store.repositories()
            for item in store.items(owner, repo)
            if item.get("closed_at")
            and start <= datetime.fromisoformat(item["closed_at"]) <= end
        ]

    async def aclose(self) -> None:
        """The files are opened per operation."""

    def _work(self, owner: str, repo: str, item: dict[str, Any]) -> Task | None:
        me = self.person.person_id
        lane = item.get("lane")
        if lane not in _LANES or me not in item.get("assignees", []):
            return None
        comments = item.get("comments", [])
        mine = bool(comments) and comments[-1]["author"] == me
        if lane == Task.READY:
            reason = None if mine else "ready_lane"
        elif comments:
            reason = None if mine else "issue_comment"
        else:
            reason = "working_lane"
        if reason is None:
            return None
        return store.task(owner, repo, item, lane, assignee=me, trigger_reason=reason)
