"""Each provider of the factory, ready for the ports' contracts.

A provider's harness gives the code host and the tickets of one member, and
what a contract cannot do through the ports: a branch on the remote, a new
commit on it, an artifact of a commit, an assignment on the board. The
harness speaks as the ports do -- items by their URLs, comments and reviews by
the ids their reads give -- and names its repository, its tracker, and a
repository outside the member's scope by the paths its provider spells.

GitHub answers from :class:`GitHubDouble`, a GitHub that keeps what it is sent
and checks that what a request names belongs to what the path says; Branvil
from :class:`~tests.guildbotics.integrations.branvil_double.BranvilDouble`,
which does the same by Branvil's API, with tickets numbered in the
repository and a board; ``local``
keeps its files under the test's workspace, with a repository three names deep
and tickets keyed ``T-<n>`` in a tracker of their own.

Each provider also holds a ticket outside the member's scope, with one
comment: someone else's, readable, and not the member's to write to.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any

import httpx

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.integrations.github import async_client, github_utils
from guildbotics.integrations.github.repository_scope import NODE_REPOSITORY
from guildbotics.integrations.local import store
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_service import TicketService
from tests.guildbotics.integrations import branvil_double
from tests.guildbotics.local_code_host import tracker, write

MEMBER = "aiko"
LOGIN = "aiko-gh"
API = "https://api.github.com"
#: A ticket URL of no provider of the factory.
FOREIGN_TICKET = "https://tickets.example.com/browse/OPS-7"
#: The one label every tracker defines.
LABEL = "bug"


@dataclass
class Harness:
    name: str
    code: CodeHostingService
    tickets: TicketService
    #: The member's repository, as its provider spells it.
    repository: str
    #: Where the member's tickets are kept.
    tracker: str
    #: A repository outside the member's scope.
    elsewhere: str
    #: A tracker outside the member's scope.
    their_tracker: str
    #: Make ``branch`` of the repository's remote one commit ahead of ``main``.
    branch: Callable[[str], None]
    #: Put one more commit on ``branch``.
    advance: Callable[[str], None]
    #: Delete ``branch`` of the repository's remote.
    delete_branch: Callable[[str], None]
    #: Put the ticket in the board's ready lane, assigned to the member.
    assign: Callable[[str], None]
    #: Keep the CI artifact ``name`` of the commit ``sha``.
    artifact: Callable[[str, str, bytes], None]
    #: A ticket outside the member's scope, as the board's candidate.
    theirs: Task
    #: The id of its comment.
    their_comment: str
    #: The URL of a ticket of the member's tracker that is not there.
    missing_ticket: str
    #: The optional features the provider has; it refuses the others.
    features: frozenset[str]
    #: The writes that reached the provider, as ``"<what> <where>"``.
    writes: list[str] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.code.aclose()
        await self.tickets.aclose()


def harness(name: str, tmp_path: Path, monkeypatch) -> Harness:
    return {"branvil": _branvil, "github": _github, "local": _local}[name](
        tmp_path, monkeypatch
    )


def _services(name: str, scope: str, **board: str) -> tuple[Any, Any]:
    person = Person(
        person_id=MEMBER,
        name="Aiko",
        person_type="agent",
        account_info={"github_username": LOGIN, "branvil_username": MEMBER},
    )
    project = Project(
        name="demo",
        services={
            "code_hosting_service": {"name": name, "owner": scope},
            "ticket_service": {"name": name, "owner": scope, **board},
        },
    )
    team = Team(project=project, members=[person])
    factory = ServiceIntegrationFactory()
    logger = logging.getLogger(__name__)
    return (
        factory.create_code_hosting_service(logger, person, team),
        factory.create_ticket_service(logger, person, team),
    )


# -- local ---------------------------------------------------------------


def _local(tmp_path: Path, monkeypatch) -> Harness:
    scope = "acme/team"
    repository, tickets_tracker = f"{scope}/demo", f"{scope}/demo-tickets"
    bare = store.bare(repository)
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "seed")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    tracker(tickets_tracker, repository, labels=(LABEL,))

    def commit(name: str, start: str | None) -> None:
        if start is None:
            _git(seed, "switch", "-q", name)
        else:
            _git(seed, "switch", "-q", "-c", name, start)
        (seed / f"{name.replace('/', '-')}.txt").write_text(
            f"{name} {next(revisions)}", encoding="utf-8"
        )
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", name)
        _git(seed, "push", "-q", str(bare), name)

    revisions = count()

    def delete_branch(name: str) -> None:
        _git(seed, "push", "-q", str(bare), "--delete", name)
        _git(seed, "switch", "-q", "main")
        _git(seed, "branch", "-q", "-D", name)

    def assign(url: str) -> None:
        ref = store.locate(url, "ticket")
        ticket = store.load(ref)
        ticket.update(lane=Task.READY, assignees=[MEMBER])
        write(ref, ticket)

    def artifact(sha: str, name: str, content: bytes) -> None:
        path = store.artifact(repository, sha, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    their_tracker = "other/team/tickets"
    tracker(their_tracker, None)
    theirs_ref = store.ref_of("ticket", their_tracker, 1)
    write(
        theirs_ref,
        {
            "number": 1,
            "title": "Theirs",
            "body": "",
            "state": "open",
            "author": "them",
            "labels": [],
            "assignees": [],
            "lane": Task.READY,
            "created_at": store.now(),
            "closed_at": None,
            "comments": [
                {
                    "id": "comment-1",
                    "author": "them",
                    "body": "Hi",
                    "created_at": store.now(),
                }
            ],
        },
    )

    writes: list[str] = []
    save = store.save

    def saved(scope: str, ref: store.Ref, item: dict[str, Any]) -> None:
        save(scope, ref, item)
        writes.append(f"save {ref.url}")

    monkeypatch.setattr(store, "save", saved)
    code, tickets = _services("local", scope)
    return Harness(
        "local",
        code,
        tickets,
        repository,
        tickets_tracker,
        "other/team/demo",
        their_tracker,
        lambda name: commit(name, "main"),
        lambda name: commit(name, None),
        delete_branch,
        assign,
        artifact,
        Task(
            id=theirs_ref.url,
            url=theirs_ref.url,
            title="Theirs",
            description="",
            status=Task.READY,
        ),
        "comment-1",
        store.ref_of("ticket", tickets_tracker, 404).url,
        frozenset({"request_changes", "artifacts"}),
        writes,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Seed",
            "-c",
            "user.email=seed@example.com",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


# -- Branvil -------------------------------------------------------------


def _branvil(tmp_path: Path, monkeypatch) -> Harness:
    double = branvil_double.BranvilDouble()
    branvil_double.serve(double, monkeypatch)
    monkeypatch.setenv(f"{MEMBER.upper()}_BRANVIL_TOKEN", branvil_double.TOKEN)
    code, tickets = _services(
        "branvil", branvil_double.SCOPE, board=branvil_double.BOARD
    )
    return Harness(
        "branvil",
        code,
        tickets,
        branvil_double.REPO,
        branvil_double.REPO,
        branvil_double.THEIR_REPO,
        branvil_double.THEIR_REPO,
        double.branch,
        double.advance,
        double.branches.pop,
        double.assign,
        double.add_artifact,
        Task(
            id=double.their_ticket,
            url=double.their_ticket,
            title="Theirs",
            description="",
            status=Task.READY,
        ),
        double.their_comment,
        f"{branvil_double.BASE}/{branvil_double.REPO}/-/tickets/404",
        frozenset({"request_changes", "artifacts"}),
        double.writes,
    )


# -- GitHub --------------------------------------------------------------

OWNER = "acme"
REPO = f"{OWNER}/demo"
THEIRS = "other-owner/demo"


def _github(tmp_path: Path, monkeypatch) -> Harness:
    double = GitHubDouble()
    serve(double, monkeypatch)
    code, tickets = _services(
        "github",
        OWNER,
        project_id="1",
        url=f"https://github.com/orgs/{OWNER}/projects/1",
    )
    theirs = f"https://github.com/{THEIRS}/issues/1"
    return Harness(
        "github",
        code,
        tickets,
        REPO,
        REPO,
        THEIRS,
        THEIRS,
        double.branch,
        double.advance,
        double.branches.pop,
        double.assign,
        double.add_artifact,
        Task(
            id=_THEIR_NODE,
            url=theirs,
            title="Theirs",
            description="",
            status=Task.READY,
        ),
        "issuecomment-1",
        f"https://github.com/{REPO}/issues/404",
        frozenset({"request_changes", "artifacts", "dependency_alerts"}),
        double.writes,
    )


def serve(double: GitHubDouble, monkeypatch) -> None:
    """Have every member's GitHub client, gate included, talk to ``double``,
    with a token for ``MEMBER``."""

    async def create(person, base_url, owner):
        client = async_client.get_async_client(
            base_url, github_utils.GitHubTokenAuth("token"), owner
        )
        client._transport = httpx.MockTransport(double.respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(
        "guildbotics.integrations.github.api.create_github_client", create
    )
    monkeypatch.setenv(f"{MEMBER.upper()}_GITHUB_ACCESS_TOKEN", "token")


#: The node of issue #1 of ``THEIRS``, and of its comment.
_THEIR_NODE = "THEIRS"
_THEIR_COMMENT = "THEIRS_COMMENT"
_ISSUE_LINK = re.compile(r"(?:Closes|Refs) #(\d+)")


class GitHubDouble:
    """GitHub, as far as the contracts ask it: it keeps the issues, pull
    requests, comments, reviews, branches, artifacts and Project items it is
    sent, and answers a comment or review only at an address of its own."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.items: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.review_comments: dict[int, dict[str, Any]] = {}
        self.reviews: dict[int, dict[str, Any]] = {}
        self.branches: dict[str, str] = {"main": "base-sha"}
        self.artifacts: dict[int, dict[str, Any]] = {}
        #: The Project's items: issue number -> Status option name.
        self.board: dict[int, str] = {}
        #: Each issue's cross-references: the pull requests that name it.
        self.timeline: dict[int, list[int]] = {}
        self._ids = count(100)
        self._revisions = count(1)

    def branch(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha"

    def advance(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha{next(self._revisions)}"
        for item in self.items.values():
            if (item.get("head") or {}).get("ref") == name:
                item["head"]["sha"] = self.branches[name]

    def assign(self, url: str) -> None:
        number = int(url.rsplit("/", 1)[1])
        self.items[number]["assignees"] = [{"login": LOGIN}]
        self.board[number] = "Todo"

    def add_artifact(self, sha: str, name: str, content: bytes) -> None:
        artifact_id = next(self._ids)
        self.artifacts[artifact_id] = {
            "id": artifact_id,
            "name": name,
            "size_in_bytes": len(content),
            "expired": False,
            "workflow_run": {"id": 7, "head_sha": sha},
            "content": content,
        }

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method != "GET" and (
            path != "/graphql" or "mutation" in body["query"]
        ):
            self.writes.append(f"{request.method} {path}")
        if path == "/graphql":
            return httpx.Response(200, json={"data": self._graphql(body)})
        if path == "/rate_limit":
            return httpx.Response(200, json={"resources": {}})
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if match is None:
            return httpx.Response(404, json={"message": "Not Found"})
        route = match.group(2) or ""
        if match.group(1) != REPO:
            return self._theirs(request.method, route)
        if match := re.fullmatch(r"/actions/artifacts/(\d+)/zip", route):
            artifact = self.artifacts.get(int(match.group(1)))
            if artifact is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, content=artifact["content"])
        answer = self._rest(request.method, route, body, request.url.params)
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if answer == ():
            return httpx.Response(204)
        return httpx.Response(201 if request.method == "POST" else 200, json=answer)

    def _theirs(self, method: str, route: str) -> httpx.Response:
        """Another owner's repository: readable, and holding one issue."""
        if method != "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        issue = {
            "number": 1,
            "node_id": _THEIR_NODE,
            "title": "Theirs",
            "state": "open",
            "user": {"login": "them"},
            "html_url": f"https://github.com/{THEIRS}/issues/1",
        }
        comment = {
            "id": 1,
            "node_id": _THEIR_COMMENT,
            "issue_url": f"{API}/repos/{THEIRS}/issues/1",
        }
        answers = {
            "": {"default_branch": "main"},
            "/pulls": [],
            "/labels": [{"name": LABEL}],
            "/issues/1": issue,
            "/issues/comments/1": comment,
        }
        if route not in answers:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=answers[route])

    def _rest(self, method: str, route: str, body: dict, params) -> Any:
        if (method, route) == ("GET", ""):
            return {"default_branch": "main"}
        if (method, route) == ("GET", "/labels"):
            return [{"name": LABEL}]
        if (method, route) == ("POST", "/issues"):
            return self._new("issue", body)
        if (method, route) == ("POST", "/pulls"):
            return self._new("pull_request", body)
        if (method, route) == ("GET", "/pulls"):
            return [
                item
                for item in self.items.values()
                if "head" in item
                and item["state"] == "open"
                and f"{OWNER}:{item['head']['ref']}" == params.get("head")
                and params.get("base") in (None, item["base"]["ref"])
            ]
        if match := re.fullmatch(r"/(issues|pulls)/(\d+)", route):
            item = self.items.get(int(match.group(2)))
            if item is None or (match.group(1) == "pulls" and "head" not in item):
                return None
            if method == "PATCH":
                item.update(body)
                if body.get("state") == "closed":
                    item["closed_at"] = "2026-07-10T01:00:00Z"
            return item
        if match := re.fullmatch(r"/issues/(\d+)/labels(?:/(.+))?", route):
            return self._labels(method, int(match.group(1)), match.group(2), body)
        if match := re.fullmatch(r"/issues/(\d+)/comments", route):
            number = int(match.group(1))
            if number not in self.items:
                return None
            if method == "POST":
                comment = self._entry(
                    body, issue=number, issue_url=f"{API}/repos/{REPO}/issues/{number}"
                )
                self.comments[comment["id"]] = comment
                return comment
            return [c for c in self.comments.values() if c["issue"] == number]
        if match := re.fullmatch(r"/issues/(\d+)/timeline", route):
            return [
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            **self.items[pull],
                            "pull_request": {"merged_at": None},
                        }
                    },
                }
                for pull in self.timeline.get(int(match.group(1)), [])
            ]
        if match := re.fullmatch(r"/issues/comments/(\d+)", route):
            return self.comments.get(int(match.group(1)))
        if match := re.fullmatch(r"/pulls/(\d+)/reviews", route):
            return self._reviews(method, int(match.group(1)), body)
        if match := re.fullmatch(r"/pulls/\d+/reviews/(\d+)", route):
            # Whichever pull request the path names: the provider checks it.
            return self.reviews.get(int(match.group(1)))
        if match := re.fullmatch(r"/pulls/(\d+)/comments", route):
            if method == "GET":
                return []
            comment = self._entry(
                body,
                pull_request_url=f"{API}/repos/{REPO}/pulls/{match.group(1)}",
                in_reply_to_id=None,
            )
            self.review_comments[comment["id"]] = comment
            return comment
        if match := re.fullmatch(r"/pulls/comments/(\d+)", route):
            return self.review_comments.get(int(match.group(1)))
        if match := re.fullmatch(r"/pulls/(\d+)/comments/(\d+)/replies", route):
            root = self.review_comments.get(int(match.group(2)))
            if root is None:
                return None
            reply = self._entry(
                body,
                pull_request_url=root["pull_request_url"],
                in_reply_to_id=root["id"],
            )
            self.review_comments[reply["id"]] = reply
            return reply
        if re.fullmatch(r"/pulls/\d+/files", route):
            return []
        if match := re.fullmatch(r"/branches/(.+)", route):
            sha = self.branches.get(match.group(1))
            return None if sha is None else {"commit": {"sha": sha}}
        if re.fullmatch(r"/compare/.+", route):
            return {"behind_by": 0}
        if re.fullmatch(r"/commits/[^/]+/check-runs", route):
            return {
                "check_runs": [
                    {"name": "ci", "status": "completed", "conclusion": "success"}
                ]
            }
        if re.fullmatch(r"/commits/[^/]+/status", route):
            return {"statuses": []}
        if route == "/actions/artifacts":
            return {
                "artifacts": [
                    {k: v for k, v in artifact.items() if k != "content"}
                    for artifact in self.artifacts.values()
                    if artifact["name"] == params.get("name")
                ]
            }
        if route == "/dependabot/alerts":
            return []
        return None

    def _labels(self, method: str, number: int, name: str | None, body: dict) -> Any:
        item = self.items.get(number)
        if item is None:
            return None
        names = [label["name"] for label in item["labels"]]
        if method == "DELETE":
            if name not in names:
                return None  # GitHub: the issue does not carry it
            item["labels"] = [
                label for label in item["labels"] if label["name"] != name
            ]
            return ()
        item["labels"] += [
            {"name": label} for label in body["labels"] if label not in names
        ]
        return item["labels"]

    def _reviews(self, method: str, number: int, body: dict) -> Any:
        if "head" not in self.items.get(number, {}):
            return None
        if method == "GET":
            return [
                review
                for review in self.reviews.values()
                if review["pull_request_url"].endswith(f"/pulls/{number}")
            ]
        review = {
            **self._entry(body, pull_request_url=f"{API}/repos/{REPO}/pulls/{number}"),
            "state": {
                "APPROVE": "APPROVED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "COMMENT": "COMMENTED",
            }[body["event"]],
            "commit_id": body["commit_id"],
            "submitted_at": "2026-07-02T00:00:00Z",
        }
        self.reviews[review["id"]] = review
        return review

    def _new(self, kind: str, body: dict) -> dict[str, Any]:
        number = len(self.items) + 1
        collection = "pull" if kind == "pull_request" else "issues"
        item: dict[str, Any] = {
            "number": number,
            "node_id": f"NODE{number}",
            "title": body["title"],
            "body": body.get("body"),
            "state": "open",
            "html_url": f"https://github.com/{REPO}/{collection}/{number}",
            "user": {"login": LOGIN},
            "labels": [{"name": name} for name in body.get("labels", [])],
            "assignees": [],
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:00:00Z",
            "closed_at": None,
        }
        if kind == "pull_request":
            item.update(
                head={
                    "ref": body["head"],
                    "sha": self.branches[body["head"]],
                    "repo": {"full_name": REPO},
                },
                base={"ref": body["base"]},
                draft=body["draft"],
                pull_request={},
                merged_at=None,
            )
            for linked in _ISSUE_LINK.findall(body.get("body") or ""):
                self.timeline.setdefault(int(linked), []).append(number)
        self.items[number] = item
        return item

    def _entry(self, body: dict, **fields: Any) -> dict[str, Any]:
        entry_id = next(self._ids)
        return {
            "id": entry_id,
            "node_id": f"NODE_C{entry_id}",
            "body": body.get("body", ""),
            "user": {"login": LOGIN},
            "created_at": "2026-07-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}#{entry_id}",
            **fields,
        }

    def _graphql(self, body: dict) -> dict[str, Any]:
        query, variables = body["query"], body.get("variables") or {}
        if query == NODE_REPOSITORY:
            theirs = variables["id"] in {_THEIR_NODE, _THEIR_COMMENT}
            owner, name = (THEIRS if theirs else REPO).split("/")
            return {"node": {"repository": {"name": name, "owner": {"login": owner}}}}
        if "addReaction" in query:
            return {"addReaction": {"reaction": {"content": variables["content"]}}}
        if "projectV2(number" in query:
            return {"organization": {"projectV2": {"id": "PROJECT"}}}
        if "addProjectV2ItemById" in query:
            number = int(variables["content"].removeprefix("NODE"))
            self.board.setdefault(number, "")
            return {"addProjectV2ItemById": {"item": {"id": f"ITEM{number}"}}}
        if "updateProjectV2ItemFieldValue" in query:
            number = int(variables["item"].removeprefix("ITEM"))
            self.board[number] = {"todo": "Todo", "working": "In Progress"}[
                variables["opt"]
            ]
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}
        if "fields(first" in query:
            return {"node": {"fields": {"nodes": _FIELDS, "pageInfo": _LAST}}}
        if "items(first" in query:
            return {"node": {"items": {"nodes": self._project(), "pageInfo": _LAST}}}
        if "projectItems(first" in query:
            number = variables["number"]
            status = self.board.get(number)
            nodes = (
                []
                if status is None
                else [
                    {
                        "project": {"id": "PROJECT"},
                        "fieldValueByName": {"name": status} if status else None,
                    }
                ]
            )
            items = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
            return {"repository": {"issue": {"projectItems": items}}}
        raise AssertionError(f"unexpected GraphQL: {query}")

    def _project(self) -> list[dict[str, Any]]:
        nodes = []
        for number, status in self.board.items():
            item = self.items[number]
            nodes.append(
                {
                    "fieldValues": {
                        "nodes": [{"field": {"name": "Status"}, "name": status}]
                        if status
                        else []
                    },
                    "content": {
                        "__typename": "Issue",
                        "id": item["node_id"],
                        "number": number,
                        "url": item["html_url"],
                        "title": item["title"],
                        "body": item["body"],
                        "createdAt": item["created_at"],
                        "state": item["state"].upper(),
                        "closedAt": item["closed_at"],
                        "assignees": {"nodes": item["assignees"]},
                        "timelineItems": {"nodes": []},
                        "labels": {"nodes": []},
                        "repository": {"name": "demo", "owner": {"login": OWNER}},
                    },
                }
            )
        return nodes


_LAST = {"hasNextPage": False, "endCursor": None}
_FIELDS = [
    {
        "id": "status",
        "name": "Status",
        "options": [
            {"id": "todo", "name": "Todo"},
            {"id": "working", "name": "In Progress"},
            {"id": "done", "name": "Done"},
        ],
    },
]
