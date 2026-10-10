"""The gate every request of a member's GitHub client passes before it is sent.

Requests go through a real ``get_async_client`` into a ``MockTransport``, so
what is asserted is what would have reached GitHub.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from guildbotics.integrations import repository_scope
from guildbotics.integrations.github import async_client, github_utils
from guildbotics.integrations.github.repository_scope import (
    NODE_MUTATIONS,
    NODE_REPOSITORY,
    PROJECT_MUTATIONS,
)
from guildbotics.integrations.repository_scope import (
    RepositoryScopeError,
    check_repository,
)

API = "https://api.github.com"
GHES = "https://ghe.example.com/api/v3"


@pytest.fixture
def sent() -> list[httpx.Request]:
    return []


@pytest.fixture
def refusals(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        repository_scope,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    return recorded


def _client(
    monkeypatch: pytest.MonkeyPatch,
    sent: list[httpx.Request],
    *,
    base_url: str = API,
    owner: str = "acme",
    auth: httpx.Auth | None = None,
    node: object = None,
) -> httpx.AsyncClient:
    """``node`` is what GitHub answers to ``NODE_REPOSITORY``, or its response."""

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "fresh", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if b"RepositoryNode" in request.content:
            if isinstance(node, httpx.Response):
                return node
            return httpx.Response(200, json={"data": {"node": node}})
        return httpx.Response(200, json={})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        async_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    return async_client.get_async_client(
        base_url, auth or github_utils.GitHubTokenAuth("token"), owner
    )


def _graphql(document: object) -> dict[str, Any]:
    return {"json": {"query": document, "variables": {}}}


PERMITTED = [
    ("GET", "/repos/other-owner/demo/issues", {}),
    ("HEAD", "/repos/other-owner/demo", {}),
    ("GET", "/rate_limit", {}),
    ("POST", "/repos/acme/demo/issues", {"json": {"title": "t"}}),
    ("POST", "/repos/ACME/demo/issues/1/comments", {"json": {"body": "b"}}),
    ("PATCH", "/repos/acme/demo/pulls/1", {"json": {"title": "t"}}),
    ("PUT", "/repos/acme/demo.github.io/contents/a", {"json": {}}),
    ("DELETE", "/repos/acme/demo/issues/1/labels/bug", {}),
    ("POST", "/app/installations/42/access_tokens", {}),
    ("POST", "/graphql", _graphql("query { viewer { login } }")),
    ("POST", "/graphql", _graphql("{ viewer { login } }")),
    *[("POST", "/graphql", _graphql(document)) for document in PROJECT_MUTATIONS],
]

REFUSED = [
    ("POST", "/repos/other-owner/demo/issues", {"json": {}}),
    ("POST", "/repos/acme-other/demo/issues", {"json": {}}),
    ("PATCH", "/repos/other/demo/pulls/1", {"json": {}}),
    ("DELETE", "/repos/other/demo/issues/1/labels/bug", {}),
    ("POST", "/repos/acme/../other/demo/issues", {"json": {}}),
    ("POST", "/repos/acme%2F..%2F..%2Frepos%2Fother/demo/issues", {"json": {}}),
    ("POST", "/repos/acme/%2E%2E/issues", {"json": {}}),
    ("POST", "/repos/acme", {"json": {}}),
    ("PUT", "/user/starred/other/demo", {}),
    ("POST", "/user/repos", {"json": {}}),
    ("POST", "/orgs/other/repos", {"json": {}}),
    ("POST", "/markdown", {"json": {}}),
    ("OPTIONS", "/repos/other/demo", {}),
    ("POST", "/app/installations/42/access_tokens/extra", {}),
    (
        "POST",
        "/graphql",
        _graphql("mutation { addComment(input: {}) { clientMutationId } }"),
    ),
    (
        "POST",
        "/graphql",
        _graphql("query { a }\nmutation { deleteIssue(input: {}) { x } }"),
    ),
    ("POST", "/graphql", _graphql(next(iter(PROJECT_MUTATIONS)) + " ")),
    ("POST", "/graphql", _graphql(None)),
    ("POST", "/graphql", {"json": [{"query": "{ viewer { login } }"}]}),
    ("POST", "/graphql", {"content": b"not json"}),
]


@pytest.mark.parametrize(("method", "path", "body"), PERMITTED)
@pytest.mark.asyncio
async def test_reads_and_writes_to_the_owner_are_sent(
    monkeypatch, sent, refusals, method, path, body
):
    client = _client(monkeypatch, sent)
    try:
        await client.request(method, path, **body)
    finally:
        await client.aclose()

    assert [request.method for request in sent] == [method]
    assert refusals == []


@pytest.mark.parametrize(("method", "path", "body"), REFUSED)
@pytest.mark.asyncio
async def test_other_writes_are_refused_before_they_are_sent(
    monkeypatch, sent, refusals, method, path, body
):
    client = _client(monkeypatch, sent)
    try:
        with pytest.raises(RepositoryScopeError, match="'acme'"):
            await client.request(method, path, **body)
    finally:
        await client.aclose()

    assert sent == []
    [refusal] = refusals
    assert refusal["event_type"] == "github.scope_refused"
    assert refusal["payload"]["scope_owner"] == "acme"
    assert refusal["payload"]["target"].startswith(f"{method} /")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/repos/acme/demo",
        "http://api.github.com/repos/acme/demo",
        "https://api.github.com:8443/repos/acme/demo",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_nothing_is_sent_to_another_host_than_the_api(
    monkeypatch, sent, refusals, method, url
):
    """The path is judged below the API's own host; the member's credential
    goes nowhere else, not even to read."""
    client = _client(monkeypatch, sent)
    try:
        with pytest.raises(RepositoryScopeError):
            await client.request(method, url, json={})
    finally:
        await client.aclose()

    assert sent == []
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_without_a_configured_owner_every_repository_write_is_refused(
    monkeypatch, sent, refusals
):
    client = _client(monkeypatch, sent, owner="")
    try:
        await client.get("/repos/acme/demo")
        with pytest.raises(RepositoryScopeError, match="No owner is configured"):
            await client.post("/repos/acme/demo/issues", json={})
    finally:
        await client.aclose()

    assert [request.method for request in sent] == ["GET"]


@pytest.mark.parametrize(
    ("method", "path", "permitted"),
    [
        ("POST", "/repos/acme/demo/issues", True),
        ("POST", "/repos/other/demo/issues", False),
        ("POST", "/repos/acme/../../other/demo/issues", False),
        ("POST", "/graphql", True),
        # How ``GitHubAppAuth`` spells the renewal: without the base path.
        ("POST", "https://ghe.example.com/app/installations/42/access_tokens", True),
    ],
)
@pytest.mark.asyncio
async def test_enterprise_server_paths_are_judged_below_the_api_base_path(
    monkeypatch, sent, refusals, method, path, permitted
):
    client = _client(monkeypatch, sent, base_url=GHES)
    body = _graphql("{ viewer { login } }") if path == "/graphql" else {}
    try:
        if permitted:
            await client.request(method, path, **body)
        else:
            with pytest.raises(RepositoryScopeError):
                await client.request(method, path, **body)
    finally:
        await client.aclose()

    assert len(sent) == int(permitted)


def _github_app_auth() -> github_utils.GitHubAppAuth:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = object.__new__(github_utils.GitHubAppAuth)
    auth.app_id = "1"
    auth.installation_id = "42"
    auth.person_id = "alice"
    auth._private_key = key
    auth._token = None
    auth._expires_at = None
    auth._leeway = dt.timedelta(seconds=120)
    return auth


@pytest.mark.asyncio
async def test_a_github_app_renews_its_token_through_the_gate(
    monkeypatch, sent, refusals
):
    client = _client(monkeypatch, sent, auth=_github_app_auth())
    try:
        await client.post("/repos/acme/demo/issues", json={})
        with pytest.raises(RepositoryScopeError):
            await client.post("/repos/other/demo/issues", json={})
    finally:
        await client.aclose()

    assert [(request.method, request.url.path) for request in sent] == [
        ("POST", "/app/installations/42/access_tokens"),
        ("POST", "/repos/acme/demo/issues"),
    ]
    assert sent[1].headers["Authorization"] == "token fresh"
    assert len(refusals) == 1


@pytest.mark.parametrize(
    ("owner", "repository"),
    [("acme", "demo"), ("Acme", "demo.js"), ("ACME", "a_b-c")],
)
def test_check_repository_admits_the_configured_owner(refusals, owner, repository):
    check_repository("acme", owner, repository)

    assert refusals == []


@pytest.mark.parametrize(
    ("scope", "owner", "repository"),
    [
        ("acme", "other-owner", "demo"),
        ("acme", "acme", ".."),
        ("acme", "acme", ""),
        ("acme", "https://example.com/acme/demo.git", ""),
        ("", "acme", "demo"),
    ],
)
def test_check_repository_refuses_anything_else(refusals, scope, owner, repository):
    with pytest.raises(RepositoryScopeError):
        check_repository(scope, owner, repository)

    [refusal] = refusals
    assert refusal["event_type"] == "github.scope_refused"


def test_project_mutations_are_mutations_of_the_configured_project() -> None:
    """The allowlist is exactly the four Project operations the issue names."""
    operations = {
        match.group(1)
        for document in PROJECT_MUTATIONS
        if (match := re.search(r"\)\s*\{\s*(\w+)", document))
    }

    assert operations == {
        "addProjectV2ItemById",
        "updateProjectV2ItemFieldValue",
        "updateProjectV2Field",
        "createProjectV2Field",
    }


#: Each write that names a node, with the variable carrying it.
NODE_WRITES = pytest.mark.parametrize(
    ("document", "variable"),
    list(NODE_MUTATIONS.items()),
    ids=list(NODE_MUTATIONS.values()),
)


def _node_write(document: str, variables: object) -> dict[str, Any]:
    return {"json": {"query": document, "variables": variables}}


def _subject(owner: str, name: str = "demo") -> dict[str, Any]:
    return {"repository": {"name": name, "owner": {"login": owner}}}


def test_node_mutations_are_a_reaction_and_the_patrols_draft_conversion() -> None:
    operations = {
        re.search(r"\{\s*(\w+)\(", document).group(1): variable
        for document, variable in NODE_MUTATIONS.items()
    }

    assert operations == {
        "addReaction": "subject",
        "convertPullRequestToDraft": "pullRequest",
    }


@NODE_WRITES
@pytest.mark.parametrize("owner", ["acme", "ACME"])
@pytest.mark.asyncio
async def test_a_node_write_is_sent_once_its_node_is_in_the_owners_repository(
    monkeypatch, sent, refusals, owner, document, variable
):
    client = _client(monkeypatch, sent, node=_subject(owner))
    try:
        await client.post(
            "/graphql", **_node_write(document, {variable: "N_1", "content": "EYES"})
        )
    finally:
        await client.aclose()

    lookup, write = sent
    assert json.loads(lookup.content) == {
        "query": NODE_REPOSITORY,
        "variables": {"id": "N_1"},
    }
    assert json.loads(write.content)["query"] == document
    assert refusals == []


@NODE_WRITES
@pytest.mark.parametrize(
    "node",
    [
        _subject("other-owner"),
        _subject("acme", ".."),
        {},  # a node outside any repository
        None,  # no such node
        {"repository": {"name": 1, "owner": {"login": "acme"}}},
        "N_1",
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["acme"]),
    ],
    ids=[
        "other_owner",
        "bad_name",
        "not_in_a_repository",
        "missing",
        "not_a_name",
        "not_a_node",
        "not_json",
        "not_an_object",
    ],
)
@pytest.mark.asyncio
async def test_a_node_write_outside_the_owner_is_refused_before_it_is_sent(
    monkeypatch, sent, refusals, node, document, variable
):
    client = _client(monkeypatch, sent, node=node)
    try:
        with pytest.raises(RepositoryScopeError, match="'acme'"):
            await client.post(
                "/graphql",
                **_node_write(document, {variable: "N_1", "content": "EYES"}),
            )
    finally:
        await client.aclose()

    [lookup] = sent
    assert json.loads(lookup.content)["query"] == NODE_REPOSITORY
    assert len(refusals) == 1


@NODE_WRITES
@pytest.mark.asyncio
async def test_a_node_write_without_its_node_is_refused_unread(
    monkeypatch, sent, refusals, document, variable
):
    """Only the variable the document names carries the node."""
    client = _client(monkeypatch, sent, node=_subject("acme"))
    try:
        for variables in ({}, {"other": "N_1"}, {variable: 1}, None, ["N_1"]):
            with pytest.raises(RepositoryScopeError):
                await client.post("/graphql", **_node_write(document, variables))
    finally:
        await client.aclose()

    assert sent == []
    assert len(refusals) == 5
