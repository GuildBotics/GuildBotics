"""Keep GitHub REST collections behind the shared pagination boundary."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[3]

# Every low-level REST GET belongs here. Each value records why a raw request
# is correct: it reads one resource, is deliberately bounded, wraps the HTTP
# transport, or is the one request issued by the shared pagination boundary.
# Adding a raw GET requires both a classification and a concrete reason.
EXPECTED_REST_GET_CLASSIFICATIONS = {
    (
        "guildbotics/app_api/avatar.py",
        "get_github_avatar_url",
        "client.get",
        "f'https://api.github.com/users/{github_username}'",
    ): "single_resource: one GitHub username identifies one user",
    (
        "guildbotics/capabilities/member_github.py",
        "context",
        "client.get",
        "'/rate_limit'",
    ): "single_resource: GitHub exposes one rate-limit status document",
    (
        "guildbotics/capabilities/member_github.py",
        "issue_update",
        "client.get",
        "issue_endpoint",
    ): "single_resource: issue_endpoint identifies one issue",
    (
        "guildbotics/capabilities/member_github.py",
        "_issue_state",
        "client.get",
        "f'/repos/{resource.owner}/{resource.repo}/issues/{resource.number}'",
    ): "single_resource: owner, repository, and issue number identify one issue",
    (
        "guildbotics/capabilities/member_github.py",
        "open_pr_checks",
        "client.get",
        "f'/repos/{owner}/{repo}/pulls'",
    ): "bounded: the configured repository and exact head branch select member-owned PRs",
    (
        "guildbotics/capabilities/member_github.py",
        "pr_create",
        "client.get",
        "endpoint",
    ): "bounded: exact head and base are queried only to find one reusable PR",
    (
        "guildbotics/capabilities/member_github.py",
        "default_branch",
        "client.get",
        "f'/repos/{owner}/{repo}'",
    ): "single_resource: owner and repository identify one repository",
    (
        "guildbotics/capabilities/member_github.py",
        "_item",
        "client.get",
        "f'/repos/{resource.owner}/{resource.repo}/{collection}/{resource.number}'",
    ): "single_resource: owner, repository, kind, and number identify one item",
    (
        "guildbotics/capabilities/member_github.py",
        "_pull_request_freshness",
        "client.get",
        'f"/repos/{resource.owner}/{resource.repo}/compare/'
        "{quote(base_sha, safe='')}...{quote(head_sha, safe='')}\"",
    ): "single_resource: the base and head pair identify one comparison",
    (
        "guildbotics/capabilities/member_github.py",
        "_pull_request_current_base_sha",
        "client.get",
        'f"/repos/{resource.owner}/{resource.repo}/branches/'
        "{quote(branch, safe='')}\"",
    ): "single_resource: owner, repository, and branch name identify one branch",
    (
        "guildbotics/integrations/github/app_manifest.py",
        "get_page",
        "client.get",
        "endpoint",
    ): "paginated: get_page is passed to paginated_items for app installations",
    (
        "guildbotics/integrations/github/github_ticket_manager.py",
        "_get_pull_request_from_url",
        "client.get",
        "f'/repos/{owner}/{repo}/pulls/{number}'",
    ): "single_resource: owner, repository, and pull request number identify one PR",
    (
        "guildbotics/integrations/github/github_ticket_manager.py",
        "_search_pull_requests",
        "client.get",
        "'/search/issues'",
    ): "bounded: oldest updated matches are intentionally processed first per qualifier",
    (
        "guildbotics/integrations/github/github_ticket_manager.py",
        "add_comment_to_ticket",
        "client.get",
        "f'{self._get_issue_path(task.repository)}/{issue_number}'",
    ): "single_resource: repository and issue number identify the comment target",
    (
        "guildbotics/integrations/github/actions_client.py",
        "_redirect_or_body",
        "self._get",
        "endpoint",
    ): "single_resource: downloads inspect one redirect or response body",
    (
        "guildbotics/integrations/github/actions_client.py",
        "_get",
        "self._client.get",
        "endpoint",
    ): "transport: _get centralizes HTTP error conversion for Actions requests",
    (
        "guildbotics/integrations/github/github_utils.py",
        "paginated_items",
        "get_page",
        "endpoint",
    ): "paginated: this is the shared page request for every REST collection",
    (
        "guildbotics/editions/simple/setup_service.py",
        "resolve_github_user",
        "requests.get",
        "f'https://api.github.com/users/{api_username}'",
    ): "single_resource: one GitHub username identifies one user",
}


def _expression_shape(source: str) -> str:
    return ast.dump(ast.parse(source, mode="eval").body, annotate_fields=False)


EXPECTED_REST_GETS = Counter(
    (*entry[:3], _expression_shape(entry[3]))
    for entry in EXPECTED_REST_GET_CLASSIFICATIONS
)


class _RestGetVisitor(ast.NodeVisitor):
    def __init__(self, path: str, *, provider_source: bool) -> None:
        self.path = path
        self.provider_source = provider_source
        self.functions: list[str] = []
        self.calls: Counter[tuple[str, str, str, str]] = Counter()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Await(self, node: ast.Await) -> None:
        call = node.value
        if isinstance(call, ast.Call):
            callee = ""
            if isinstance(call.func, ast.Attribute) and call.func.attr in {
                "get",
                "_get",
            }:
                callee = ast.unparse(call.func)
            elif isinstance(call.func, ast.Name) and call.func.id == "get_page":
                callee = call.func.id
            endpoint = _request_endpoint(call)
            if (
                callee
                and endpoint is not None
                and (self.provider_source or _is_github_rest_endpoint(endpoint))
            ):
                self.calls[
                    (
                        self.path,
                        self.functions[-1],
                        callee,
                        ast.dump(endpoint, annotate_fields=False),
                    )
                ] += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and node.func.attr == "get"
            and (endpoint := _request_endpoint(node)) is not None
            and _is_github_rest_endpoint(endpoint)
        ):
            self.calls[
                (
                    self.path,
                    self.functions[-1],
                    ast.unparse(node.func),
                    ast.dump(endpoint, annotate_fields=False),
                )
            ] += 1
        self.generic_visit(node)


def _request_endpoint(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"endpoint", "url"}
        ),
        None,
    )


def _is_github_rest_endpoint(endpoint: ast.expr) -> bool:
    text = "".join(
        node.value
        for node in ast.walk(endpoint)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return "api.github.com" in text or text.startswith(
        ("/repos/", "/app/", "/search/", "/rate_limit")
    )


def _imports_github_integration(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and bool(node.module)
        and node.module.startswith("guildbotics.integrations.github")
        for node in ast.walk(tree)
    )


def _github_rest_sources() -> list[tuple[str, ast.AST, bool]]:
    sources: list[tuple[str, ast.AST, bool]] = []
    provider_root = ROOT / "guildbotics" / "integrations" / "github"
    for path in sorted((ROOT / "guildbotics").rglob("*.py")):
        if path.name == "_version.py":
            continue
        relative_path = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        provider_source = path.is_relative_to(
            provider_root
        ) or _imports_github_integration(tree)
        sources.append((relative_path, tree, provider_source))
    return sources


def test_every_github_rest_get_is_classified_or_paginated():
    observed: Counter[tuple[str, str, str, str]] = Counter()
    for relative_path, tree, provider_source in _github_rest_sources():
        visitor = _RestGetVisitor(relative_path, provider_source=provider_source)
        visitor.visit(tree)
        observed.update(visitor.calls)

    assert observed == EXPECTED_REST_GETS
    assert all(
        classification.startswith(
            ("single_resource:", "bounded:", "transport:", "paginated:")
        )
        and classification.partition(":")[2].strip()
        for classification in EXPECTED_REST_GET_CLASSIFICATIONS.values()
    )


def test_github_rest_get_finder_accepts_a_keyword_url_outside_provider_modules():
    tree = ast.parse(
        """
async def load(client, owner, repo):
    return await client.get(url=f"/repos/{owner}/{repo}/milestones")
"""
    )
    visitor = _RestGetVisitor("guildbotics/app_api/example.py", provider_source=False)

    visitor.visit(tree)

    assert visitor.calls == Counter(
        {
            (
                "guildbotics/app_api/example.py",
                "load",
                "client.get",
                _expression_shape('f"/repos/{owner}/{repo}/milestones"'),
            ): 1
        }
    )
