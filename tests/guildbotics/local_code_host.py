"""Members working with the local code host and tickets, and the items a test
puts there for them (``guildbotics.integrations.local``).

The repository is three names deep and its tickets are keyed ``T-<n>``, so a
test reads nothing of another service's shape into the local one.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.local import store
from guildbotics.integrations.local.code_hosting import LocalCodeHostingService
from guildbotics.integrations.local.tickets import LocalTicketService
from tests.guildbotics.member_services import MemberServices

SCOPE = "acme/team"
REPOSITORY = f"{SCOPE}/demo"
#: The tracker of the repository's tickets.
TRACKER = f"{SCOPE}/demo-tickets"


def local_team(person: Person, **code: str) -> Team:
    """A team whose code host and tickets are local, written in ``SCOPE``."""
    return Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "local", "owner": SCOPE, **code},
                "ticket_service": {"name": "local", "owner": SCOPE},
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
        LocalTicketService(logging.getLogger(__name__), person, team),
    )


def remote_with(*branches: str, repository: str = REPOSITORY) -> None:
    """The repository's remote, with ``main`` and ``branches`` one commit
    ahead."""
    bare = store.bare(repository)
    seed = bare.parent / "seed"
    git = ["git", "-c", "user.name=S", "-c", "user.email=s@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "a").write_text("a", encoding="utf-8")
    subprocess.run([*git, "add", "a"], cwd=seed, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "a"], cwd=seed, check=True)
    for branch in branches:
        subprocess.run(
            [*git, "switch", "-q", "-c", branch, "main"], cwd=seed, check=True
        )
        (seed / branch.replace("/", "-")).write_text(branch, encoding="utf-8")
        subprocess.run([*git, "add", "."], cwd=seed, check=True)
        subprocess.run([*git, "commit", "-q", "-m", branch], cwd=seed, check=True)
    subprocess.run([*git, "switch", "-q", "main"], cwd=seed, check=True)
    subprocess.run([*git, "clone", "-q", "--bare", str(seed), str(bare)], check=True)


def tracker(
    place: str = TRACKER,
    repository: str | None = REPOSITORY,
    labels: tuple[str, ...] = (),
) -> None:
    """Keep the tracker ``place``, whose tickets belong to ``repository``."""
    path = store.directory("tickets", place) / "tracker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repository": repository, "labels": list(labels)}),
        encoding="utf-8",
    )


def ticket(number: int = 1, *, place: str = TRACKER, **fields: Any) -> str:
    """Put ticket ``T-<number>`` in the tracker; its URL."""
    if not (store.directory("tickets", place) / "tracker.json").is_file():
        tracker(place)
    ref = store.ref_of("ticket", place, number)
    write(
        ref,
        {
            "number": number,
            "title": f"Ticket {number}",
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
    return ref.url


def pull_request(
    number: int, head: str, *, repository: str = REPOSITORY, **fields: Any
) -> str:
    """Put pull request ``!<number>`` of the repository from ``head``; its URL."""
    ref = store.ref_of("pull_request", repository, number)
    write(
        ref,
        {
            "number": number,
            "title": f"Pull request {number}",
            "body": "",
            "state": "open",
            "author": "someone",
            "labels": [],
            "assignees": [],
            "created_at": store.now(),
            "closed_at": None,
            "comments": [],
            "head": head,
            "head_repository": repository,
            "base": "main",
            "draft": False,
            "merged": False,
            "reviewers": [],
            "reviews": [],
            "review_comments": [],
            "tickets": [],
            **fields,
        },
    )
    return ref.url


def comment(author: str, body: str = "a comment", **fields: Any) -> dict[str, Any]:
    return {
        "id": fields.pop("id", f"comment-{900 + len(body)}"),
        "author": author,
        "body": body,
        "created_at": fields.pop("created_at", store.now()),
        **fields,
    }


def item(url: str) -> dict[str, Any]:
    """The pull request or ticket ``url`` names, as its file keeps it."""
    kind = "pull_request" if "://code/" in url else "ticket"
    return store.load(store.locate(url, kind))


def write(ref: store.Ref, item: dict[str, Any]) -> None:
    """Put ``item`` where ``ref`` keeps it, as someone editing the files would:
    no member writes it, so no member's scope applies."""
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_text(json.dumps(item), encoding="utf-8")
