"""Each provider of the factory, ready for the ports' contracts.

A provider's harness gives the code host and the board of one member of a
project owned by ``acme``, and what a contract cannot do through the ports --
a branch on the remote, an assignment on the board, the URL of an item of
another owner. GitHub answers from :class:`GitHubDouble`, a GitHub that keeps
what it is sent; ``local`` keeps its files under the test's workspace.

Each provider also holds issue #1 of ``other-owner/demo``, with comment 1:
someone else's, readable, and not the member's to write to.
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
from guildbotics.runtime.ticket_manager import TicketManager
from tests.guildbotics.local_code_host import write

OWNER = "acme"
REPO = f"{OWNER}/demo"
THEIRS = "other-owner/demo"
MEMBER = "aiko"
LOGIN = "aiko-gh"
API = "https://api.github.com"


@dataclass
class Harness:
    name: str
    code: CodeHostingService
    board: TicketManager
    #: Make ``branch`` of acme/demo's remote one commit ahead of ``main``.
    branch: Callable[[str], None]
    #: Delete ``branch`` of acme/demo's remote.
    delete_branch: Callable[[str], None]
    #: Put the issue in the board's ready lane, assigned to the member.
    assign: Callable[[str], None]
    #: The URL of ``kind`` ``number`` of ``owner/repo``.
    url: Callable[[str, str, str, int], str]
    #: Issue #1 of ``THEIRS``, as the board's ticket.
    theirs: Task
    #: The writes that reached the provider, as ``"<what> <where>"``.
    writes: list[str] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.code.aclose()
        await self.board.aclose()


def harness(name: str, tmp_path: Path, monkeypatch) -> Harness:
    return {"github": _github, "local": _local}[name](tmp_path, monkeypatch)


def _team(name: str, **code: str) -> tuple[Person, Team]:
    person = Person(
        person_id=MEMBER,
        name="Aiko",
        person_type="agent",
        account_info={"github_username": LOGIN},
    )
    board = (
        {
            "name": name,
            "owner": OWNER,
            "project_id": "1",
            "url": f"https://github.com/orgs/{OWNER}/projects/1",
        }
        if name == "github"
        else {"name": name}
    )
    project = Project(
        name="demo",
        services={
            "code_hosting_service": {"name": name, "owner": OWNER, **code},
            "ticket_manager": board,
        },
    )
    return person, Team(project=project, members=[person])


def _services(name: str) -> tuple[CodeHostingService, TicketManager]:
    person, team = _team(name)
    factory = ServiceIntegrationFactory()
    logger = logging.getLogger(__name__)
    return (
        factory.create_code_hosting_service(logger, person, team),
        factory.create_ticket_manager(logger, person, team),
    )


# -- local ---------------------------------------------------------------


def _local(tmp_path: Path, monkeypatch) -> Harness:
    bare = store.bare(OWNER, "demo")
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "seed")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    def branch(name: str) -> None:
        _git(seed, "switch", "-q", "-c", name, "main")
        (seed / f"{name.replace('/', '-')}.txt").write_text(name, encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", name)
        _git(seed, "push", "-q", str(bare), name)

    def delete_branch(name: str) -> None:
        _git(seed, "push", "-q", str(bare), "--delete", name)
        _git(seed, "branch", "-q", "-D", name)

    def assign(url: str) -> None:
        ref = store.locate(url, "issue")
        item = store.load(ref.owner, ref.repo, ref.number)
        item.update(lane=Task.READY, assignees=[MEMBER])
        write(ref.owner, ref.repo, item)

    owner, repo = THEIRS.split("/")
    theirs: dict[str, Any] = {
        "kind": "issue",
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
            {"id": 1, "author": "them", "body": "Hi", "created_at": store.now()}
        ],
    }
    write(owner, repo, theirs)

    writes: list[str] = []
    save = store.save

    def saved(project: Project, owner: str, repo: str, item: dict[str, Any]) -> None:
        save(project, owner, repo, item)
        writes.append(f"save {owner}/{repo}")

    monkeypatch.setattr(store, "save", saved)
    code, board = _services("local")
    return Harness(
        "local",
        code,
        board,
        branch,
        delete_branch,
        assign,
        lambda owner, repo, kind, number: store.url(owner, repo, kind, number),  # type: ignore[arg-type]
        store.task(owner, repo, theirs, Task.READY),
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


# -- GitHub --------------------------------------------------------------


def _github(tmp_path: Path, monkeypatch) -> Harness:
    double = GitHubDouble()
    serve(double, monkeypatch)
    code, board = _services("github")
    theirs = f"https://github.com/{THEIRS}/issues/1"
    return Harness(
        "github",
        code,
        board,
        double.branch,
        double.branches.pop,
        double.assign,
        lambda owner, repo, kind, number: (
            f"https://github.com/{owner}/{repo}/"
            f"{'pull' if kind == 'pull_request' else 'issues'}/{number}"
        ),
        Task(
            id=_THEIR_NODE,
            url=theirs,
            title="Theirs",
            description="",
            status=Task.READY,
        ),
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

    for module in ("pull_requests", "github_ticket_manager"):
        monkeypatch.setattr(
            f"guildbotics.integrations.github.{module}.create_github_client", create
        )
    monkeypatch.setenv(f"{MEMBER.upper()}_GITHUB_ACCESS_TOKEN", "token")


#: The node of issue #1 of ``THEIRS``.
_THEIR_NODE = "THEIRS"


class GitHubDouble:
    """GitHub, as far as the contracts ask it: it keeps the issues, pull
    requests, comments, reviews, branches and Project items it is sent."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.items: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.review_comments: dict[int, dict[str, Any]] = {}
        self.branches: dict[str, str] = {"main": "base-sha"}
        #: The Project's items: issue number -> Status option name.
        self.board: dict[int, str] = {}
        self._ids = count(100)

    def branch(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha"

    def assign(self, url: str) -> None:
        number = int(url.rsplit("/", 1)[1])
        self.items[number]["assignees"] = [{"login": LOGIN}]
        self.board[number] = "Todo"

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
            # Another owner's repository: readable, and empty.
            if request.method != "GET":
                return httpx.Response(404, json={"message": "Not Found"})
            issue = {
                "number": 1,
                "node_id": _THEIR_NODE,
                "title": "Theirs",
                "user": {"login": "them"},
            }
            answers = {"": {"default_branch": "main"}, "/pulls": [], "/issues/1": issue}
            if route not in answers:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=answers[route])
        answer = self._rest(request.method, route, body, request.url.params)
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201 if request.method == "POST" else 200, json=answer)

    def _rest(self, method: str, route: str, body: dict, params) -> Any:
        if (method, route) == ("GET", ""):
            return {"default_branch": "main"}
        if (method, route) == ("GET", "/labels"):
            return []
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
        if match := re.fullmatch(r"/issues/(\d+)/comments", route):
            number = int(match.group(1))
            if method == "POST":
                comment = self._entry(body, issue=number)
                self.comments[comment["id"]] = comment
                return comment
            return [c for c in self.comments.values() if c["issue"] == number]
        if re.fullmatch(r"/issues/\d+/timeline", route):
            return []
        if match := re.fullmatch(r"/(issues|pulls)/comments/(\d+)/reactions", route):
            return {"id": next(self._ids), "content": body["content"]}
        if match := re.fullmatch(r"/pulls/(\d+)/reviews", route):
            return {**self._entry(body), "state": "APPROVED", "submitted_at": "t"}
        if match := re.fullmatch(r"/pulls/(\d+)/comments", route):
            comment = self._entry(
                body, pull_request_url=f"{API}/repos/{REPO}/pulls/{match.group(1)}"
            )
            self.review_comments[comment["id"]] = comment
            return comment
        if match := re.fullmatch(r"/pulls/comments/(\d+)", route):
            return self.review_comments.get(int(match.group(1)))
        if match := re.fullmatch(r"/pulls/(\d+)/comments/(\d+)/replies", route):
            return self._entry(body)
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
        return None

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
        self.items[number] = item
        return item

    def _entry(self, body: dict, **fields: Any) -> dict[str, Any]:
        entry_id = next(self._ids)
        return {
            "id": entry_id,
            "body": body.get("body", ""),
            "user": {"login": LOGIN},
            "created_at": "2026-07-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}#{entry_id}",
            **fields,
        }

    def _graphql(self, body: dict) -> dict[str, Any]:
        query, variables = body["query"], body.get("variables") or {}
        if query == NODE_REPOSITORY:
            owner, name = (THEIRS if variables["id"] == _THEIR_NODE else REPO).split(
                "/"
            )
            return {"node": {"repository": {"name": name, "owner": {"login": owner}}}}
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
        "dataType": "SINGLE_SELECT",
        "options": [
            {"id": "todo", "name": "Todo"},
            {"id": "working", "name": "In Progress"},
            {"id": "done", "name": "Done"},
        ],
    },
    {
        "id": "agent",
        "name": "Agent",
        "dataType": "SINGLE_SELECT",
        "options": [{"id": "agent-aiko", "name": f"⚙{MEMBER}"}],
    },
]
