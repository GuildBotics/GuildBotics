"""Members working with the local code host and board, and the items a test
puts there for them (``guildbotics.integrations.local``)."""

from __future__ import annotations

import json
import logging
from typing import Any

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.local import store
from guildbotics.integrations.local.code_hosting import LocalCodeHostingService
from guildbotics.integrations.local.ticket_board import LocalTicketManager
from tests.guildbotics.member_services import MemberServices

OWNER = "owner"
REPO = "repo"


def local_team(person: Person, **code: str) -> Team:
    """A team whose code host and board are local, owned by ``owner``."""
    return Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "local", "owner": OWNER, **code},
                "ticket_manager": {"name": "local"},
            },
        ),
        members=[person],
    )


def local_member(person: Person | None = None, **code: str) -> MemberServices:
    person = person or Person(person_id="aiko", name="Aiko", person_type="agent")
    team = local_team(person, **code)
    return MemberServices(
        person,
        team,
        LocalCodeHostingService(person, team),
        LocalTicketManager(logging.getLogger(__name__), person, team),
    )


def issue(number: int = 1, **fields: Any) -> str:
    """Put issue ``#<number>`` of owner/repo there; its URL."""
    return _save(number, "issue", fields)


def pull_request(number: int, head: str, **fields: Any) -> str:
    """Put pull request ``#<number>`` of owner/repo from ``head`` there; its URL."""
    return _save(
        number,
        "pull_request",
        {
            "head": head,
            "head_repo": None,
            "base": "main",
            "draft": False,
            "merged": False,
            "reviewers": [],
            "reviews": [],
            "review_comments": [],
            "issue": None,
            **fields,
        },
    )


def comment(author: str, body: str = "a comment", **fields: Any) -> dict[str, Any]:
    return {
        "id": fields.pop("id", 900 + len(body)),
        "author": author,
        "body": body,
        "created_at": fields.pop("created_at", store.now()),
        **fields,
    }


def item(url: str) -> dict[str, Any]:
    ref = store.locate(url)
    return store.load(ref.owner, ref.repo, ref.number)


def write(owner: str, repo: str, item: dict[str, Any]) -> None:
    """Put ``item`` among the files of ``owner/repo``, as someone editing them
    would: no member writes it, so no member's scope applies."""
    path = store.item_path(owner, repo, item["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item), encoding="utf-8")


def _save(number: int, kind: str, fields: dict[str, Any]) -> str:
    write(
        OWNER,
        REPO,
        {
            "kind": kind,
            "number": number,
            "title": f"Item {number}",
            "body": "",
            "state": "open",
            "author": "someone",
            "labels": [],
            "assignees": [],
            "lane": None,
            "created_at": store.now(),
            "closed_at": None,
            "comments": [],
            **fields,
        },
    )
    return store.url(OWNER, REPO, kind, number)  # type: ignore[arg-type]
