"""Keep GitHub REST collections behind the shared pagination boundary."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[3]
GITHUB_REST_FILES = (
    "guildbotics/capabilities/member_github.py",
    "guildbotics/integrations/github/github_ticket_manager.py",
    "guildbotics/integrations/github/actions_client.py",
    "guildbotics/integrations/github/github_utils.py",
)

# Every low-level REST GET belongs here. Collection reads have one entry in
# paginated_items; the remaining entries are single resources or deliberately
# bounded queries. Adding another raw collection read changes this inventory.
EXPECTED_REST_GETS = Counter(
    {
        (
            "guildbotics/capabilities/member_github.py",
            "context",
            "client.get",
            "'/rate_limit'",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "issue_update",
            "client.get",
            "issue_endpoint",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "_issue_state",
            "client.get",
            "f'/repos/{resource.owner}/{resource.repo}/issues/{resource.number}'",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "open_pr_checks",
            "client.get",
            "f'/repos/{owner}/{repo}/pulls'",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "pr_create",
            "client.get",
            "endpoint",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "default_branch",
            "client.get",
            "f'/repos/{owner}/{repo}'",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "_item",
            "client.get",
            "f'/repos/{resource.owner}/{resource.repo}/{collection}/{resource.number}'",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "_pull_request_freshness",
            "client.get",
            'f"/repos/{resource.owner}/{resource.repo}/compare/'
            "{quote(base_sha, safe='')}...{quote(head_sha, safe='')}\"",
        ): 1,
        (
            "guildbotics/capabilities/member_github.py",
            "_pull_request_current_base_sha",
            "client.get",
            'f"/repos/{resource.owner}/{resource.repo}/branches/'
            "{quote(branch, safe='')}\"",
        ): 1,
        (
            "guildbotics/integrations/github/github_ticket_manager.py",
            "_get_pull_request_from_url",
            "client.get",
            "f'/repos/{owner}/{repo}/pulls/{number}'",
        ): 1,
        (
            "guildbotics/integrations/github/github_ticket_manager.py",
            "_search_pull_requests",
            "client.get",
            "'/search/issues'",
        ): 1,
        (
            "guildbotics/integrations/github/github_ticket_manager.py",
            "add_comment_to_ticket",
            "client.get",
            "f'{self._get_issue_path(task.repository)}/{issue_number}'",
        ): 1,
        (
            "guildbotics/integrations/github/actions_client.py",
            "_redirect_or_body",
            "self._get",
            "endpoint",
        ): 1,
        (
            "guildbotics/integrations/github/actions_client.py",
            "_get",
            "self._client.get",
            "endpoint",
        ): 1,
        (
            "guildbotics/integrations/github/github_utils.py",
            "paginated_items",
            "get_page",
            "endpoint",
        ): 1,
    }
)


class _RestGetVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.functions: list[str] = []
        self.calls: Counter[tuple[str, str, str, str]] = Counter()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Await(self, node: ast.Await) -> None:
        call = node.value
        if isinstance(call, ast.Call) and call.args:
            callee = ""
            if isinstance(call.func, ast.Attribute) and call.func.attr in {
                "get",
                "_get",
            }:
                callee = ast.unparse(call.func)
            elif isinstance(call.func, ast.Name) and call.func.id == "get_page":
                callee = call.func.id
            if callee:
                self.calls[
                    (
                        self.path,
                        self.functions[-1],
                        callee,
                        ast.unparse(call.args[0]),
                    )
                ] += 1
        self.generic_visit(node)


def test_every_github_rest_get_is_classified_or_paginated():
    observed: Counter[tuple[str, str, str, str]] = Counter()
    for relative_path in GITHUB_REST_FILES:
        visitor = _RestGetVisitor(relative_path)
        visitor.visit(ast.parse((ROOT / relative_path).read_text(encoding="utf-8")))
        observed.update(visitor.calls)

    assert observed == EXPECTED_REST_GETS
