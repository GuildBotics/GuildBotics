"""Every GitHub write GuildBotics makes is classified here.

``repository_scope`` refuses, before it is sent, any request of a member's
GitHub client that writes outside the configured owner, and ``member git``
checks the repository before it gives git the member's credential. What that
covers is only as good as the population it is drawn around, so the
population is taken from the code itself, not from the writes someone
happened to think of:

- every call that sends a request other than a read, in every module that
  reaches GitHub
- every HTTP client those modules make, since a client other than the gated
  one is not judged
- every caller of ``create_github_client``, since the gate is only as right
  as the owner it is given
- every use of the member's credential for git (``push_credential``), in any
  module, since it is given to git only where the repository is checked
- every GraphQL mutation document in the package

A new one fails here until it is classified.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import guildbotics
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github import async_client, github_utils
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.repository_scope import (
    CONVERT_PULL_REQUEST_TO_DRAFT,
    NODE_MUTATIONS,
    NODE_REPOSITORY,
    PROJECT_MUTATIONS,
)
from guildbotics.integrations.repository_scope import RepositoryScopeError

PACKAGE = Path(guildbotics.__file__).parent
SCOPE_MODULE = "integrations/github/repository_scope.py"

#: Sent through the gated client: a REST write under ``/repos/{owner}/{repo}``.
REPOSITORY = "gated: REST write under /repos/{owner}/{repo}"
#: Sent through the gated client: a GraphQL query or a Project mutation.
GRAPHQL = "gated: GraphQL"

#: ``(module, function, method)`` of every write, and what judges it.
WRITES = {
    ("integrations/github/code_hosting_service.py", "_read_resource", "post"): GRAPHQL,
    ("integrations/github/code_hosting_service.py", "create_issue", "post"): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "update_issue",
        "patch",
    ): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "_apply_label_changes",
        "delete",
    ): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "_apply_label_changes",
        "post",
    ): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "create_pull_request",
        "post",
    ): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "update_pull_request",
        "patch",
    ): REPOSITORY,
    ("integrations/github/code_hosting_service.py", "review", "post"): REPOSITORY,
    (
        "integrations/github/code_hosting_service.py",
        "review_comment",
        "post",
    ): REPOSITORY,
    ("integrations/github/code_hosting_service.py", "reply", "post"): REPOSITORY,
    ("integrations/github/code_hosting_service.py", "add_reaction", "post"): REPOSITORY,
    ("integrations/github/code_hosting_service.py", "_graphql", "post"): GRAPHQL,
    (
        "integrations/github/code_hosting_service.py",
        "_post_comment",
        "post",
    ): REPOSITORY,
    ("integrations/github/github_ticket_manager.py", "_graphql", "post"): GRAPHQL,
    ("integrations/github/repository_scope.py", "_node_in_scope", "post"): GRAPHQL,
    (
        "integrations/github/github_utils.py",
        "create_github_app_installation_token",
        "post",
    ): "issues the App's installation token for git; names no repository",
    (
        "integrations/github/app_manifest.py",
        "convert_manifest_code",
        "post",
    ): "creates the GitHub App during setup, before any member credential",
}

#: ``(module, function)`` of every HTTP client made, and why it may be one.
CLIENTS = {
    ("integrations/github/async_client.py", "get_async_client"): "the gated client",
    (
        "integrations/github/github_utils.py",
        "create_github_app_installation_token",
    ): "issues the App's installation token; its one request is that",
    (
        "integrations/github/app_manifest.py",
        "_api_client",
    ): "setup of the GitHub App, before any member credential",
    (
        "integrations/github/actions_client.py",
        "_download_without_credentials",
    ): "GETs a short-lived download URL without credentials",
    (
        "integrations/github/actions_client.py",
        "_download_tail_without_credentials",
    ): "GETs a short-lived log URL without credentials",
}

#: ``(module, function)`` of every use of a member's credential for git, in
#: any module: the code host hands it out (``push_credential``).
TOKEN_USES = {
    ("capabilities/member_git.py", "prepare"): "fetch only; a read",
    ("capabilities/member_git.py", "push"): "checked",
}
#: Where GitHub makes the credential git is handed.
GITHUB_TOKENS = {("integrations/github/code_hosting_service.py", "push_credential")}

_READS = {"GET", "HEAD"}
_WRITE_METHODS = {"post", "patch", "put", "delete"}
_METHOD_ARGUMENT = {"request", "stream"}
_CLIENT_TYPES = {"AsyncClient", "Client"}
_MUTATION = re.compile(r"(?m)^\s*mutation\b")


def _modules() -> Iterator[tuple[str, ast.Module]]:
    """Every module that reaches GitHub, with its tree."""
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        source = path.read_text(encoding="utf-8")
        if name.startswith("integrations/github/") or (
            "guildbotics.integrations.github" in source
        ):
            yield name, ast.parse(source)


def _calls(tree: ast.Module) -> Iterator[tuple[str, ast.Call, list[ast.AST]]]:
    """Every call, the function it is in, and its ancestors (innermost first)."""

    def walk(node: ast.AST, function: str, ancestors: list[ast.AST]):
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            if isinstance(child, ast.Call):
                yield inner, child, [node, *ancestors]
            yield from walk(child, inner, [node, *ancestors])

    yield from walk(tree, "", [])


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _writes() -> dict[tuple[str, str, str], ast.Call]:
    found = {}
    for module, tree in _modules():
        for function, call, _ in _calls(tree):
            method = _callee(call)
            if not isinstance(call.func, ast.Attribute):
                continue
            if method in _METHOD_ARGUMENT:
                verb = call.args[0] if call.args else None
                if isinstance(verb, ast.Constant) and str(verb.value) in _READS:
                    continue
            elif method not in _WRITE_METHODS:
                continue
            found[(module, function, method)] = call
    return found


def test_every_write_to_github_is_classified() -> None:
    assert set(_writes()) == set(WRITES)


def test_every_http_client_is_classified() -> None:
    found = {
        (module, function)
        for module, tree in _modules()
        for function, call, _ in _calls(tree)
        if _callee(call) in _CLIENT_TYPES
    }

    assert found == set(CLIENTS)


def test_every_github_client_is_given_the_configured_owner() -> None:
    """The owner is the configured one, never the one a request targets.

    Passing the target's owner would make the gate agree with every write.
    """
    callers = set()
    for module, tree in _modules():
        configured = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _callee(node.value) == "configured_owner"
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        for function, call, _ in _calls(tree):
            if _callee(call) not in {"create_github_client", "get_async_client"}:
                continue
            if module == "integrations/github/github_utils.py":
                continue  # create_github_client handing its own owner on
            owner = call.args[2] if len(call.args) > 2 else None
            owner = owner or next(
                (kw.value for kw in call.keywords if kw.arg == "owner"), None
            )
            assert owner is not None, (module, function)
            assert (
                isinstance(owner, ast.Call) and _callee(owner) == "configured_owner"
            ) or (isinstance(owner, ast.Attribute) and owner.attr in configured), (
                module,
                function,
                ast.unparse(owner),
            )
            callers.add((module, function))

    assert callers == {
        ("integrations/github/pull_requests.py", "_get_client"),
        ("integrations/github/github_ticket_manager.py", "login"),
    }


def test_every_git_push_is_checked_before_git_is_given_the_token() -> None:
    uses = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).as_posix()
        for function, call, ancestors in _calls(ast.parse(path.read_text("utf-8"))):
            if _callee(call) != "push_credential":
                continue
            uses.add((module, function))
            if TOKEN_USES.get((module, function)) != "checked":
                continue
            assert _checked_before(call, ancestors), (module, function, call.lineno)

    assert uses == set(TOKEN_USES)
    assert {
        (module, function)
        for module, tree in _modules()
        for function, call, _ in _calls(tree)
        if _callee(call) == "get_person_github_token"
    } == GITHUB_TOKENS


def test_git_is_given_the_token_only_where_its_destination_is_checked() -> None:
    """``_Origin._connected`` refuses a URL git's configuration rewrites; a
    credential handed to git anywhere else would go wherever that says."""
    tree = ast.parse((PACKAGE / "capabilities/member_git.py").read_text("utf-8"))

    assert [
        function
        for function, call, _ in _calls(tree)
        if _callee(call) == "_git_auth_environment"
    ] == ["_connected"]


def _checked_before(call: ast.Call, ancestors: list[ast.AST]) -> bool:
    """Whether ``check_repository`` runs in a statement before ``call``'s,
    in its block or a block around it."""
    child: ast.AST = call
    for parent in ancestors:
        for body in (
            value
            for _, value in ast.iter_fields(parent)
            if isinstance(value, list) and child in value
        ):
            for statement in body[: body.index(child)]:
                if any(
                    isinstance(node, ast.Call) and _callee(node) == "check_repository"
                    for node in ast.walk(statement)
                ):
                    return True
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
            return False
        child = parent
    return False


def test_every_graphql_mutation_is_one_the_gate_knows() -> None:
    """The Project's own operations, or a write whose node the gate reads."""
    found = {
        (module, node.value)
        for module, tree in (
            (path.relative_to(PACKAGE).as_posix(), ast.parse(path.read_text("utf-8")))
            for path in sorted(PACKAGE.rglob("*.py"))
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _MUTATION.search(node.value)
    }

    assert found == {
        (SCOPE_MODULE, document) for document in {*PROJECT_MUTATIONS, *NODE_MUTATIONS}
    }


@pytest.mark.asyncio
async def test_a_reaction_on_a_review_outside_the_owner_is_refused_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reaction add --target pr-review`` names the review by repository, but
    the mutation names only its node: the gate reads where the node lives."""
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews/55"):
            return httpx.Response(200, json={"node_id": "PRR_55"})
        body = json.loads(request.content)
        sent.append(body)
        repository = {"name": "demo", "owner": {"login": "other-owner"}}
        return httpx.Response(200, json={"data": {"node": {"repository": repository}}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        async_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    project = Project(
        name="demo",
        services={"code_hosting_service": {"name": "github", "owner": "acme"}},
    )
    service = GitHubCodeHostingService(person, Team(project=project, members=[person]))
    service._client = async_client.get_async_client(
        "https://api.github.com", github_utils.GitHubTokenAuth("token"), "acme"
    )
    try:
        with pytest.raises(RepositoryScopeError, match="'acme'"):
            await service.add_reaction("other-owner/demo", "pr-review", 55, "eyes", 7)
    finally:
        await service.aclose()

    assert [body["query"] for body in sent] == [NODE_REPOSITORY]


@pytest.mark.asyncio
async def test_a_draft_conversion_outside_the_owner_is_refused_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patrol converts a PR to a draft by its node: the gate reads where the
    node lives before the code host's mutation goes out."""
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        repository = {"name": "demo", "owner": {"login": "other-owner"}}
        return httpx.Response(200, json={"data": {"node": {"repository": repository}}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        async_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="agent",
        account_info={"github_username": "aiko-gh"},
    )
    project = Project(
        name="demo",
        services={"code_hosting_service": {"name": "github", "owner": "acme"}},
    )
    service = GitHubCodeHostingService(person, Team(project=project, members=[person]))
    service._client = async_client.get_async_client(
        "https://api.github.com", github_utils.GitHubTokenAuth("token"), "acme"
    )
    try:
        with pytest.raises(RepositoryScopeError, match="'acme'"):
            await service._graphql(
                CONVERT_PULL_REQUEST_TO_DRAFT, {"pullRequest": "PR_1"}
            )
    finally:
        await service.aclose()

    assert sent == [{"query": NODE_REPOSITORY, "variables": {"id": "PR_1"}}]
