"""The GitHub provider's services over a scripted GitHub.

The services are built as the factory builds them, and their client is the
member's real one (``get_async_client``: the write gate and the response hook
included); only its transport is replaced, by :class:`ScriptedGitHub`, which
answers each request from the routes a test gives it and keeps what was sent.
What a test asserts is therefore what would have reached GitHub, and a failure
reaches the provider the way GitHub's would.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github import async_client, github_utils
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.ticket_service import GitHubTicketService

MEMBER = "aiko"
LOGIN = "aiko-gh"
OWNER = "acme"
REPO = f"{OWNER}/demo"
API = "https://api.github.com"
WEB = "https://github.com"
PROJECT_URL = f"{WEB}/orgs/{OWNER}/projects/1"

#: What a route answers: a JSON body, a response, or a function of the
#: request returning either.
Answer = Any


def issue_url(number: int, repository: str = REPO) -> str:
    return f"{WEB}/{repository}/issues/{number}"


def pull_url(number: int, repository: str = REPO) -> str:
    return f"{WEB}/{repository}/pull/{number}"


class ScriptedGitHub:
    """A GitHub that answers from routes and keeps every request it is sent.

    REST routes are keyed by method and path; a GraphQL request is answered by
    the first route whose marker its document contains, with that route's
    ``data`` (or a function of the variables returning it). Anything else is
    GitHub's 404.
    """

    def __init__(self) -> None:
        self.rest: dict[tuple[str, str], Answer] = {}
        self.graph: list[tuple[str, Answer]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, answer: Answer) -> ScriptedGitHub:
        self.rest[(method, path)] = answer
        return self

    def on_graphql(self, marker: str, data: Answer) -> ScriptedGitHub:
        self.graph.append((marker, data))
        return self

    @property
    def sent(self) -> list[str]:
        """Every request as ``"<METHOD> <path>"``, GraphQL ones included."""
        return [f"{r.method} {r.url.path}" for r in self.requests]

    @property
    def writes(self) -> list[str]:
        """The requests that are not reads, GraphQL queries left out."""
        return [
            f"{r.method} {r.url.path}"
            for r in self.requests
            if r.method not in {"GET", "HEAD"}
            and (r.url.path != "/graphql" or "mutation" in _body(r)["query"])
        ]

    def bodies(self, method: str, path: str) -> list[Any]:
        """The JSON bodies sent with ``method`` to ``path``."""
        return [
            _body(r) for r in self.requests if r.method == method and r.url.path == path
        ]

    def documents(self) -> list[dict[str, Any]]:
        """Every GraphQL request body, in order."""
        return self.bodies("POST", "/graphql")

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/graphql":
            body = _body(request)
            for marker, data in self.graph:
                if marker in body["query"]:
                    if callable(data):
                        data = data(body.get("variables") or {})
                    if isinstance(data, httpx.Response):
                        return data
                    return httpx.Response(200, json={"data": data})
            return httpx.Response(200, json={"errors": [{"type": "NOT_FOUND"}]})
        answer = self.rest.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        if answer == ():
            return httpx.Response(204)
        return httpx.Response(201 if request.method == "POST" else 200, json=answer)


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


def serve(github: ScriptedGitHub, monkeypatch) -> None:
    """Have every member's GitHub client talk to ``github``."""

    async def create(person, base_url, owner):
        client = async_client.get_async_client(
            base_url, github_utils.GitHubTokenAuth("token"), owner
        )
        client._transport = httpx.MockTransport(github.respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(
        "guildbotics.integrations.github.api.create_github_client", create
    )


def member(**account: str) -> Person:
    return Person(
        person_id=MEMBER,
        name="Aiko",
        person_type="agent",
        account_info={"github_username": LOGIN, **account},
    )


def team(person: Person, *others: Person, **board: Any) -> Team:
    project = Project(
        name="demo",
        services={
            "code_hosting_service": {"name": "github", "owner": OWNER},
            "ticket_service": {
                "name": "github",
                "owner": OWNER,
                "project_id": "1",
                "url": PROJECT_URL,
                **board,
            },
        },
    )
    return Team(project=project, members=[person, *others])


def code_host(
    github: ScriptedGitHub, monkeypatch, person: Person | None = None
) -> GitHubCodeHostingService:
    serve(github, monkeypatch)
    person = person or member()
    return GitHubCodeHostingService(person, team(person))


def ticket_service(
    github: ScriptedGitHub,
    monkeypatch,
    person: Person | None = None,
    *others: Person,
    **board: Any,
) -> GitHubTicketService:
    serve(github, monkeypatch)
    person = person or member()
    return GitHubTicketService(
        logging.getLogger(__name__), person, team(person, *others, **board)
    )


def pages(items: list[Any], page_size: int = 100) -> Callable[[httpx.Request], Any]:
    """A REST collection of ``items``, answered page by page."""

    def answer(request: httpx.Request) -> Any:
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("per_page", str(page_size)))
        return items[(page - 1) * size : page * size]

    return answer
