"""How GitHub's code host and tickets read: fixed REST routes and GraphQL
documents, never a caller's query, and GitHub's answers as the ports' shapes.

A page past the first is asked for with a continuation, which carries the
request it continues and GitHub's cursor: it is data, never a URL.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from guildbotics.integrations.diff_lines import file_change
from guildbotics.integrations.github.api import (
    INLINE,
    REVIEW,
    GitHubApi,
    GitHubItem,
    comment_id,
    graphql_error,
)
from guildbotics.integrations.github.async_client import (
    GitHubNotFound,
    GitHubResponseError,
)
from guildbotics.integrations.github.pull_requests import head_of, head_sha_of
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.runtime.resources import (
    MAX_PAGE_BYTES,
    DependencyAlert,
    ReadPage,
)
from guildbotics.utils.i18n_tool import t

_CURSOR_LIMIT = 2048
_CONTINUATION_LIMIT = 8192
#: The REST route of each collection of an item.
_REST = {
    "ticket_comments": "{conversation}/comments",
    # A pull request's conversation comments are issue comments on GitHub.
    "pull_request_comments": "{conversation}/comments",
    "ticket_pull_requests": "{conversation}/timeline",
    "pull_request_reviews": "{api}/reviews",
    "pull_request_files": "{api}/files",
}
#: The routes that answer for an issue and a pull request alike: the item's
#: kind is read before each of their pages.
_SHARED_ROUTES = frozenset(
    {"ticket_comments", "pull_request_comments", "ticket_pull_requests"}
)
_VERDICTS = {
    "APPROVED": "approve",
    "CHANGES_REQUESTED": "request_changes",
    "COMMENTED": "comment",
    "DISMISSED": "dismissed",
}
_PERMISSIONS = {
    "dependency_alerts": "Dependabot alerts",
    "tickets": "Issues / Projects",
    "ticket_comments": "Issues",
    "ticket_pull_requests": "Issues / Pull requests",
    "pull_request_comments": "Pull requests",
    "pull_requests": "Pull requests",
    "pull_request_reviews": "Pull requests",
    "pull_request_files": "Pull requests",
    "pull_request_threads": "Pull requests",
    "review_thread_comments": "Pull requests",
    "pull_request_readiness": "Pull requests / Contents / Checks / Commit statuses / Actions",
}
_COMMENTS = """nodes { databaseId body createdAt url author { login }
replyTo { databaseId } } pageInfo { endCursor hasNextPage }"""
_THREADS = (
    "query($owner:String!, $repo:String!, $number:Int!, $after:String, $size:Int!) {"
    "repository(owner:$owner, name:$repo) { pullRequest(number:$number) {"
    "reviewThreads(first:$size, after:$after) { nodes { "
    "id isResolved isOutdated comments(first:1) { " + _COMMENTS + " } }"
    "pageInfo { endCursor hasNextPage } } } } }"
)
_THREAD_COMMENTS = (
    "query($owner:String!, $repo:String!, $number:Int!, $node:ID!, $after:String,"
    " $size:Int!) {"
    "repository(owner:$owner, name:$repo) { pullRequest(number:$number) { id } }"
    "node(id:$node) { ... on PullRequestReviewThread { pullRequest { id }"
    "comments(first:$size, after:$after) { " + _COMMENTS + " } } } }"
)


@contextmanager
def reading(resource: str) -> Iterator[None]:
    """Tell a failed read of ``resource`` with the permission it needs, and an
    answer of an unexpected shape as GitHub's.

    Raises:
        ServiceError: For either.
    """
    try:
        yield
    except GitHubResponseError as exc:
        error = exc.needing(_PERMISSIONS[resource])
        if resource == "dependency_alerts" and isinstance(exc, GitHubNotFound):
            raise ServiceNotFound(
                f"{error} {t('integrations.github.response.alerts_disabled')}"
            ) from None
        raise error from None
    except AttributeError, TypeError, ValueError, LookupError:
        raise ServiceError(t("integrations.github.response.response")) from None


def request_of(
    api: GitHubApi, resource: str, subject: str, conditions: dict[str, Any]
) -> dict[str, Any]:
    """What a continuation continues."""
    return {
        "resource": resource,
        "subject": subject,
        "parameters": conditions,
        "api_base_url": api.base_url,
    }


async def read_collection(
    api: GitHubApi,
    resource: str,
    item: GitHubItem,
    request: dict[str, Any],
    continuation: str,
) -> ReadPage:
    """A page of the collection ``resource`` of ``item``."""
    cursor = _cursor(continuation, request) if continuation else ""
    conditions = request["parameters"]
    if resource in {"pull_request_threads", "review_thread_comments"}:
        return await _graph_page(api, resource, item, conditions, cursor, request)
    if cursor and not re.fullmatch(r"[1-9][0-9]{0,5}", cursor):
        raise ServiceError(t("integrations.resources.continuation"))
    if resource in _SHARED_ROUTES:
        await api.fetch(item)
    client = await api.client()
    response = await client.get(
        _REST[resource].format(api=item.api, conversation=item.conversation),
        params={"per_page": conditions["page_size"], "page": cursor or "1"},
        follow_redirects=False,
        extensions={"max_response_bytes": MAX_PAGE_BYTES},
    )
    data = response.json()
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise ValueError("Unexpected page")
    next_cursor = None
    if response.links.get("next"):
        pages = httpx.URL(response.links["next"]["url"]).params.get_list("page")
        if (
            len(pages) != 1
            or not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0])
            or int(pages[0]) <= int(cursor or "1")
        ):
            raise ValueError("Unexpected next page")
        next_cursor = pages[0]
    items = [
        translated
        for raw in data
        if (translated := _translate(api, resource, item, raw)) is not None
    ]
    return ReadPage.of(
        resource,
        items,
        continuation=_encode(next_cursor, request) if next_cursor else None,
    )


async def _graph_page(
    api: GitHubApi,
    resource: str,
    item: GitHubItem,
    conditions: dict[str, Any],
    cursor: str,
    request: dict[str, Any],
) -> ReadPage:
    owner, name = item.repository.split("/")
    variables: dict[str, Any] = {
        "owner": owner,
        "repo": name,
        "number": item.number,
        "after": cursor or None,
        "size": conditions["page_size"],
    }
    if resource == "review_thread_comments":
        variables["node"] = conditions["node"]
        query = _THREAD_COMMENTS
    else:
        query = _THREADS
    client = await api.client()
    response = await client.post(
        "/graphql",
        json={"query": query, "variables": variables},
        follow_redirects=False,
        extensions={"max_response_bytes": MAX_PAGE_BYTES},
    )
    payload = response.json()
    if payload.get("errors"):
        raise graphql_error(payload["errors"])
    data = payload["data"]
    pull_request = data["repository"]["pullRequest"]
    if resource == "review_thread_comments":
        node = data["node"]
        if node is None or node["pullRequest"]["id"] != pull_request["id"]:
            raise ServiceNotFound(
                f"'{conditions['node']}' is no review thread of {api.url(item)}."
            )
        connection = node["comments"]
        items = [_thread_comment(api, c) for c in connection["nodes"]]
    else:
        connection = pull_request["reviewThreads"]
        items = [_thread(api, thread) for thread in connection["nodes"]]
    info = connection["pageInfo"]
    next_cursor = None
    if info["hasNextPage"]:
        next_cursor = info["endCursor"]
        if (
            not isinstance(next_cursor, str)
            or not 0 < len(next_cursor) <= _CURSOR_LIMIT
            or next_cursor == cursor
        ):
            raise ValueError("Unexpected cursor")
    return ReadPage.of(
        resource,
        items,
        continuation=_encode(next_cursor, request) if next_cursor else None,
    )


def pull_request(
    api: GitHubApi, item: GitHubItem, data: dict[str, Any]
) -> dict[str, Any]:
    """The pull request ``data`` is, as the ports read one."""
    head = head_of(item, data)
    return {
        **api.target(item, data).model_dump(),
        "repository": item.repository,
        "body": data.get("body") or "",
        "state": data["state"],
        "assignees": [user["login"] for user in data.get("assignees") or []],
        "labels": [label["name"] for label in data.get("labels") or []],
        "author_type": api.author_type(data),
        "author": str((data.get("user") or {}).get("login") or ""),
        "requested_reviewers": [
            user["login"] for user in data.get("requested_reviewers") or []
        ],
        "head": head.branch,
        "head_repository": head.repository,
        "head_sha": head_sha_of(item, data),
        "base": (data.get("base") or {}).get("ref", ""),
        "merged": data.get("merged_at") is not None,
        "draft": bool(data.get("draft")),
        "changed_files": data.get("changed_files"),
    }


async def read_alerts(
    api: GitHubApi,
    repository: str,
    conditions: dict[str, Any],
    request: dict[str, Any],
    continuation: str,
) -> ReadPage:
    """A page of the repository's Dependabot alerts, or the one ``id`` names."""
    path = f"/repos/{repository}/dependabot/alerts"
    alert = conditions["id"]
    if alert:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", alert):
            raise ServiceNotFound(f"'{alert}' names no Dependabot alert.")
        query: dict[str, Any] = {}
        path += f"/{alert}"
    else:
        query = {
            "state": {
                "open": "open",
                "resolved": "fixed",
                "dismissed": "dismissed,auto_dismissed",
            }[conditions["state"]],
            "per_page": conditions["page_size"],
        }
        if continuation:
            query["after"] = _cursor(continuation, request)
    client = await api.client()
    response = await client.get(
        path,
        params=query,
        follow_redirects=False,
        extensions={"max_response_bytes": MAX_PAGE_BYTES},
    )
    data = response.json()
    if alert:
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Unexpected alerts")
    return ReadPage.of(
        "dependency_alerts",
        [_alert(item) for item in data],
        continuation=None if alert else _alerts_continuation(response, request),
    )


def _translate(
    api: GitHubApi, resource: str, item: GitHubItem, raw: dict[str, Any]
) -> dict[str, Any] | None:
    """One answer of a REST collection; ``None`` for one that is no item of
    the resource (a timeline event that is no pull request)."""
    if resource in {"ticket_comments", "pull_request_comments"}:
        return api.conversation_comment(raw)
    if resource == "pull_request_reviews":
        verdict = _VERDICTS.get(str(raw.get("state")))
        if verdict is None:
            return None  # a review its author has not submitted yet
        return {
            "id": comment_id(REVIEW, raw.get("id")),
            "body": str(raw.get("body") or ""),
            "author": str((raw.get("user") or {}).get("login") or ""),
            "author_type": api.author_type(raw),
            "verdict": verdict,
            "commit": raw.get("commit_id"),
            "submitted_at": raw.get("submitted_at"),
            "url": raw.get("html_url"),
        }
    if resource == "pull_request_files":
        return _file(raw)
    return _referring_pull_request(api, item, raw)


def _referring_pull_request(
    api: GitHubApi, item: GitHubItem, event: dict[str, Any]
) -> dict[str, Any] | None:
    source = (event.get("source") or {}).get("issue") or {}
    if event.get("event") != "cross-referenced" or "pull_request" not in source:
        return None
    try:
        found = api.item(str(source.get("html_url") or ""), "pull_request")
    except ServiceNotFound:
        return None  # a pull request of another GitHub
    return {
        "url": api.url(found),
        "label": found.label
        if found.repository.casefold() == item.repository.casefold()
        else f"{found.repository}{found.label}",
        "title": str(source.get("title") or ""),
        "state": str(source.get("state") or ""),
        "merged": bool((source.get("pull_request") or {}).get("merged_at")),
    }


def _thread(api: GitHubApi, thread: dict[str, Any]) -> dict[str, Any]:
    comments = thread["comments"]
    return {
        "id": thread["id"],
        "resolved": thread["isResolved"],
        "outdated": thread["isOutdated"],
        "comments": [_thread_comment(api, c) for c in comments["nodes"]],
        "comments_complete": not comments["pageInfo"]["hasNextPage"],
    }


def _thread_comment(api: GitHubApi, comment: dict[str, Any]) -> dict[str, Any]:
    login = str((comment.get("author") or {}).get("login") or "")
    reply_to = (comment.get("replyTo") or {}).get("databaseId")
    return {
        "id": comment_id(INLINE, comment.get("databaseId")),
        "body": str(comment.get("body") or ""),
        "author": login,
        "author_type": api.author_type({"user": {"login": login}}),
        "created_at": comment.get("createdAt"),
        "url": comment.get("url"),
        "reply_to_id": None if reply_to is None else comment_id(INLINE, reply_to),
    }


def _file(file: dict[str, Any]) -> dict[str, Any]:
    return file_change(
        str(file.get("filename") or ""),
        str(file.get("status") or ""),
        file.get("patch"),
        additions=file.get("additions", 0),
        deletions=file.get("deletions", 0),
        changes=file.get("changes", 0),
    )


def _alert(item: dict[str, Any]) -> DependencyAlert:
    number = item["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid alert identifier")
    dependency = item.get("dependency") or {}
    package = dependency.get("package") or {}
    advisory = item.get("security_advisory") or {}
    vulnerability = item.get("security_vulnerability") or {}
    patched = vulnerability.get("first_patched_version") or {}
    return DependencyAlert.model_validate(
        {
            "id": str(number),
            "state": {
                "open": "open",
                "fixed": "resolved",
                "dismissed": "dismissed",
                "auto_dismissed": "dismissed",
            }[item["state"]],
            "url": item.get("html_url"),
            "package": package.get("name"),
            "ecosystem": package.get("ecosystem"),
            "manifest_path": dependency.get("manifest_path"),
            "severity": vulnerability.get("severity") or advisory.get("severity"),
            "identifiers": advisory.get("identifiers") or [],
            "summary": advisory.get("summary"),
            "description": advisory.get("description"),
            "affected_versions": vulnerability.get("vulnerable_version_range"),
            "patched_version": patched.get("identifier"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
    )


def _cursor(continuation: str, request: dict[str, Any]) -> str:
    """A continuation is data, never a URL or an authorization grant."""
    try:
        if len(continuation) > _CONTINUATION_LIMIT:
            raise ValueError
        value = json.loads(
            base64.b64decode(continuation, altchars=b"-_", validate=True)
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"request", "after"}
            or value["request"] != request
            or not isinstance(value["after"], str)
            or not 0 < len(value["after"]) <= _CURSOR_LIMIT
        ):
            raise ValueError
        return value["after"]
    except ValueError, binascii.Error, UnicodeError:
        raise ServiceError(t("integrations.resources.continuation")) from None


def _encode(cursor: str, request: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": cursor}).encode()
    ).decode()


def _alerts_continuation(
    response: httpx.Response, request: dict[str, Any]
) -> str | None:
    link = response.links.get("next")
    if link is None:
        return None
    try:
        # GitHub may canonicalize the URL to /repositories/{id}/..., or omit
        # filters. Only the cursor is data we need; we never request this URL.
        cursors = httpx.URL(link["url"]).params.get_list("after")
        if len(cursors) != 1 or not 0 < len(cursors[0]) <= _CURSOR_LIMIT:
            raise ValueError
    except KeyError, ValueError, httpx.InvalidURL:
        raise ServiceError(t("integrations.resources.continuation")) from None
    return _encode(cursors[0], request)
