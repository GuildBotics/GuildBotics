"""GitHub as a member's code host: repository reads and writes, CI, the git
remote, and the pull request patrol."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import IO, Any
from urllib.parse import quote, urlparse

import httpx

from guildbotics.entities import Task
from guildbotics.integrations.chat_workflow_status import workflow_status_fields
from guildbotics.integrations.github.actions_client import (
    GitHubActionsClient,
    GitHubActionsClientError,
)
from guildbotics.integrations.github.async_client import ResponseTooLarge
from guildbotics.integrations.github.github_utils import (
    get_author_type,
    get_github_username,
    get_person_github_token,
    normalize_login,
    paginated_items,
    parse_timestamp,
)
from guildbotics.integrations.github.issue_links import (
    append_issue_link,
    preserve_issue_links,
)
from guildbotics.integrations.github.pull_request_patrol import (
    MAX_REVIEW_ROUNDS,
    PULL_REQUEST_QUERY,
    REVIEW_LIMIT,
    REVIEW_LIMIT_REASON,
    PullRequest,
    parse_pull_request,
    pull_request_work,
)
from guildbotics.integrations.github.pull_requests import (
    GITHUB_ACTIONS_RUN_MIN_PART_COUNT,
    GitHubPullRequests,
    GitHubResource,
    _raise_for_status,
    _work_target,
)
from guildbotics.integrations.github.read_resources import (
    DETAIL_RESOURCES,
    GRAPH_RESOURCES,
    REST_RESOURCES,
    graph_connection,
    graph_query,
    translate,
)
from guildbotics.integrations.github.repository_scope import (
    ADD_REACTION,
    CONVERT_PULL_REQUEST_TO_DRAFT,
)
from guildbotics.integrations.github.workflow_status_comment import (
    render_workflow_status_comment,
)
from guildbotics.runtime.code_hosting_resources import (
    MAX_PAGE_BYTES,
    DependencyAlert,
    RepositoryReadError,
    RepositoryReadPage,
    WorkTarget,
    read_conditions,
)
from guildbotics.runtime.code_hosting_service import (
    ArtifactRef,
    CodeHostingService,
    CommentWritten,
    IssueCreated,
    IssueUpdated,
    ItemKind,
    ItemRef,
    PullRequestCreated,
    PullRequestHead,
    PullRequestUpdated,
    ReactionAdded,
    ReactionTarget,
    ReplyWritten,
    ReviewCommentWritten,
    ReviewEvent,
    ReviewSubmitted,
)
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.utils.i18n_tool import t

_LOGGER = logging.getLogger(__name__)
REPO_WITH_OWNER_PART_COUNT = 2
_REVIEW_EVENTS = {
    "approve": "APPROVE",
    "request-changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
#: REST reaction names as GraphQL's ``ReactionContent`` spells them.
_GRAPHQL_REACTIONS = {
    "+1": "THUMBS_UP",
    "-1": "THUMBS_DOWN",
    "laugh": "LAUGH",
    "confused": "CONFUSED",
    "heart": "HEART",
    "hooray": "HOORAY",
    "rocket": "ROCKET",
    "eyes": "EYES",
}
#: The two kinds of URL this code host names an item with.
_KINDS: dict[ItemKind, str] = {"issue": "issue", "pull_request": "pull"}


_CURSOR_LIMIT = 2048
_CONTINUATION_LIMIT = 8192


class GitHubCodeHostingService(GitHubPullRequests, CodeHostingService):
    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        conditions = read_conditions(resource, repo, identifier, parameters)
        if resource == "dependency_alerts" and not identifier:
            conditions = _alert_query(conditions)
        request = {
            "resource": resource,
            "repo": repo,
            "identifier": identifier,
            "parameters": conditions,
            "api_base_url": self.base_url,
        }
        if resource != "dependency_alerts":
            return await self._read_resource(request, continuation)
        query = dict(conditions)
        if continuation:
            if identifier:
                raise RepositoryReadError(t("integrations.repository.continuation"))
            query["after"] = _cursor(continuation, request)
        path = f"repos/{repo}/dependabot/alerts"
        if identifier:
            path += f"/{identifier}"
        client = await self._get_client()
        try:
            response = await client.get(
                path,
                params=query,
                follow_redirects=False,
                extensions={"max_response_bytes": MAX_PAGE_BYTES},
            )
            if response.status_code != HTTPStatus.OK:
                raise _http_error(response, resource)
            data = response.json()
            if identifier:
                data = [data]
            if not isinstance(data, list):
                raise ValueError
            return RepositoryReadPage.of(
                resource,
                [_alert(item) for item in data],
                continuation=None if identifier else _continuation(response, request),
            )
        except ResponseTooLarge:
            raise RepositoryReadError(t("integrations.github.read.too_large")) from None
        except httpx.HTTPStatusError as exc:
            raise _http_error(exc.response, resource) from None
        except httpx.RequestError:
            raise RepositoryReadError(t("integrations.github.read.transport")) from None
        except (AttributeError, TypeError, ValueError, KeyError):
            raise RepositoryReadError(t("integrations.github.read.response")) from None

    async def _read_resource(
        self, request: dict[str, Any], continuation: str
    ) -> RepositoryReadPage:
        resource, repo, identifier, conditions = (
            request[k] for k in ("resource", "repo", "identifier", "parameters")
        )
        if continuation and resource in DETAIL_RESOURCES:
            raise RepositoryReadError(t("integrations.repository.continuation"))
        cursor = _cursor(continuation, request) if continuation else ""
        if (
            resource in REST_RESOURCES
            and cursor
            and not re.fullmatch(r"[1-9][0-9]{0,5}", cursor)
        ):
            raise RepositoryReadError(t("integrations.repository.continuation"))
        client = await self._get_client()
        try:
            if resource == "pull_request_readiness":
                readiness = await self.readiness(
                    f"{self.web_base_url()}/{repo}/pull/{identifier}", **conditions
                )
                return RepositoryReadPage.of(
                    resource, [readiness], target=readiness.target
                )
            next_cursor = None
            if resource in GRAPH_RESOURCES:
                owner, name = repo.split("/")
                variables = {
                    "owner": owner,
                    "repo": name,
                    "number": int(identifier),
                    "after": cursor or None,
                    "size": conditions["page_size"],
                }
                if resource == "review_thread_comments":
                    variables["node"] = conditions["node"]
                response = await client.post(
                    "graphql",
                    json={"query": graph_query(resource), "variables": variables},
                    follow_redirects=False,
                    extensions={"max_response_bytes": MAX_PAGE_BYTES},
                )
                if response.status_code != HTTPStatus.OK:
                    raise _http_error(response, resource)
                payload = response.json()
                if payload.get("errors"):
                    raise _graphql_error(payload["errors"], resource)
                connection = graph_connection(resource, payload["data"])
                data = connection["nodes"]
                info = connection["pageInfo"]
                if info["hasNextPage"]:
                    next_cursor = info["endCursor"]
                    if (
                        not isinstance(next_cursor, str)
                        or not 0 < len(next_cursor) <= _CURSOR_LIMIT
                        or next_cursor == cursor
                    ):
                        raise ValueError
            else:
                path = f"repos/{repo}/" + REST_RESOURCES[resource].format(
                    identifier=identifier
                )
                params = (
                    {}
                    if resource in DETAIL_RESOURCES
                    else {"per_page": conditions["page_size"], "page": cursor or "1"}
                )
                response = await client.get(
                    path,
                    params=params,
                    follow_redirects=False,
                    extensions={"max_response_bytes": MAX_PAGE_BYTES},
                )
                if response.status_code != HTTPStatus.OK:
                    raise _http_error(response, resource)
                data = response.json()
                if resource in DETAIL_RESOURCES:
                    data = [data]
                elif response.links.get("next"):
                    pages = httpx.URL(response.links["next"]["url"]).params.get_list(
                        "page"
                    )
                    if (
                        len(pages) != 1
                        or not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0])
                        or int(pages[0]) <= int(cursor or "1")
                    ):
                        raise ValueError
                    next_cursor = pages[0]
            if not isinstance(data, list) or any(
                not isinstance(item, dict) for item in data
            ):
                raise ValueError
            target = None
            if resource in {"issues", "pull_requests"}:
                item = data[0]
                if item.get("number") != int(identifier) or not isinstance(
                    item.get("title"), str
                ):
                    raise ValueError
                owner, name = repo.split("/")
                ref = GitHubResource(
                    owner,
                    name,
                    int(identifier),
                    "pull" if resource == "pull_requests" else "issue",
                )
                target = _work_target(ref, item)
                result = {
                    **target,
                    "body": item.get("body") or "",
                    "state": item["state"],
                    "assignees": [v["login"] for v in item.get("assignees", [])],
                    "labels": [v["name"] for v in item.get("labels", [])],
                }
                if resource == "pull_requests":
                    head = self._pull_request_head(ref, item)
                    result.update(
                        author_type=_author_type(self.person, item),
                        head=head.branch,
                        head_repo=head.full_repo,
                        head_owner=head.owner,
                        head_repo_name=head.repo,
                        head_sha=self._pull_request_head_sha(ref, item),
                        base=(item.get("base") or {}).get("ref", ""),
                        merged=item.get("merged_at") is not None,
                        draft=bool(item.get("draft")),
                        changed_files=item.get("changed_files"),
                    )
                data = [result]
            return RepositoryReadPage.of(
                resource,
                [translate(resource, item, self.person) for item in data],
                target=WorkTarget.model_validate(target) if target else None,
                continuation=_encode_cursor(next_cursor, request)
                if next_cursor
                else None,
            )
        except ResponseTooLarge:
            raise RepositoryReadError(t("integrations.github.read.too_large")) from None
        except MemberCapabilityError as exc:
            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, httpx.HTTPStatusError):
                    raise _http_error(cause.response, resource) from None
                if isinstance(cause, httpx.RequestError):
                    raise RepositoryReadError(
                        t("integrations.github.read.transport")
                    ) from None
                cause = cause.__cause__
            raise RepositoryReadError(str(exc)) from None
        except httpx.HTTPStatusError as exc:
            raise _http_error(exc.response, resource) from None
        except httpx.RequestError:
            raise RepositoryReadError(t("integrations.github.read.transport")) from None
        except (AttributeError, TypeError, ValueError, KeyError):
            raise RepositoryReadError(t("integrations.github.read.response")) from None

    # ------------------------------------------------------------------ #
    #   The member                                                       #
    # ------------------------------------------------------------------ #

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        if check:
            client = await self._get_client()
            # /rate_limit is readable by every credential type (PAT, machine
            # user, and GitHub App installation token). /user would 403 for
            # installation tokens (github_apps members), so it cannot be used
            # as a generic credential probe.
            resp = await client.get("/rate_limit")
            _raise_for_status(resp)
        return {"github_username": get_github_username(self.person)}

    def locate(self, url: str, kind: ItemKind | None = None) -> ItemRef:
        resource = self.parse_url(url, expected_kind=_KINDS[kind] if kind else None)
        return _item_ref(self.web_base_url(), resource)

    # ------------------------------------------------------------------ #
    #   Issues                                                           #
    # ------------------------------------------------------------------ #

    async def comment(
        self,
        url: str,
        body: str,
        *,
        kind: ItemKind | None = None,
        status: dict[str, object] | None = None,
    ) -> CommentWritten:
        resource = self.parse_url(url, expected_kind=_KINDS[kind] if kind else None)
        # The target is read before the write, never after it: a read that
        # fails after the comment landed would make a retry post it twice.
        item = await (
            self._pull_request(resource)
            if resource.kind == "pull"
            else self._issue(resource)
        )
        if status is not None:
            body = _addressed(
                render_workflow_status_comment(body=body, payload=status),
                str((item.get("user") or {}).get("login") or ""),
                self.person,
            )
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        user = comment.get("user") or {}
        return CommentWritten(
            comment_id=comment.get("id"),
            html_url=comment.get("html_url"),
            author=str(user.get("login") or ""),
            created_at=comment.get("created_at"),
            target=WorkTarget.model_validate(_work_target(resource, item, url)),
        )

    async def create_issue(
        self, repo: str, title: str, body: str, labels: Sequence[str]
    ) -> IssueCreated:
        owner, repo_name = self.parse_repo(repo)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = await self._defined_labels(owner, repo_name, labels)
        client = await self._get_client()
        resp = await client.post(f"/repos/{owner}/{repo_name}/issues", json=payload)
        _raise_for_status(resp)
        issue = resp.json()
        resource = GitHubResource(
            owner, repo_name, int(issue.get("number") or 0), "issue"
        )
        issue_url = (
            issue.get("html_url")
            or f"{self.web_base_url()}/{owner}/{repo_name}/issues/{resource.number}"
        )
        return IssueCreated(
            issue_number=resource.number,
            issue_title=str(issue.get("title") or title),
            repo=f"{owner}/{repo_name}",
            issue_url=issue_url,
            labels=_label_names(issue),
            target=WorkTarget.model_validate(_work_target(resource, issue, issue_url)),
        )

    async def update_issue(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        state: str | None,
        state_reason: str | None,
    ) -> IssueUpdated:
        resource = self.parse_url(url, expected_kind="issue")
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
            if state_reason is not None:
                payload["state_reason"] = state_reason
        # Label additions are validated up front so an undefined label aborts
        # before any write; the label endpoints are called only after the field
        # PATCH succeeded so a failed PATCH leaves the labels untouched.
        additions = await self._defined_labels(
            resource.owner, resource.repo, add_labels
        )
        state_changed = False
        if state is not None:
            state_changed = (await self._issue(resource)).get("state") != state
        client = await self._get_client()
        issue_endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}"
        )
        if payload:
            resp = await client.patch(issue_endpoint, json=payload)
            _raise_for_status(resp)
        await self._apply_label_changes(resource, additions, remove_labels)
        if additions or remove_labels or not payload:
            resp = await client.get(issue_endpoint)
            _raise_for_status(resp)
        issue = resp.json()
        response_body = issue.get("body")
        return IssueUpdated(
            issue_number=issue.get("number", resource.number),
            issue_url=issue.get("html_url", url),
            repo=resource.full_repo,
            title=str(issue.get("title") or ""),
            state=str(issue.get("state") or ""),
            state_changed=state_changed,
            labels=_label_names(issue),
            body="" if response_body is None else response_body,
            target=WorkTarget.model_validate(_work_target(resource, issue, url)),
        )

    async def _apply_label_changes(
        self,
        resource: GitHubResource,
        additions: Sequence[str],
        remove_labels: Sequence[str],
    ) -> None:
        """Apply label changes through the dedicated label endpoints.

        The add and remove endpoints mutate only the named labels, so labels a
        human sets concurrently survive; a PATCH of the full label set would
        overwrite them. Removing a label the issue no longer carries is a
        no-op, and a label both removed and added ends up on the issue.
        ``additions`` must already be resolved through ``_defined_labels``.
        """
        client = await self._get_client()
        labels_endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/labels"
        )
        for name in remove_labels:
            resp = await client.delete(f"{labels_endpoint}/{quote(name, safe='')}")
            if resp.status_code != HTTPStatus.NOT_FOUND:
                _raise_for_status(resp)
        if additions:
            resp = await client.post(labels_endpoint, json={"labels": list(additions)})
            _raise_for_status(resp)

    async def _defined_labels(
        self, owner: str, repo: str, labels: Sequence[str]
    ) -> list[str]:
        """Map requested labels onto the labels the repository already defines.

        Labels are a shared vocabulary, so a member picks from the repository's
        own set and proposes a missing label to a human rather than creating
        one as a side effect of an issue write.
        """
        if not labels:
            return []
        items = await self._paginated_rest_items(f"/repos/{owner}/{repo}/labels")
        defined = [name for name in (str(i.get("name") or "") for i in items) if name]
        by_name = {name.casefold(): name for name in defined}
        resolved: list[str] = []
        for label in labels:
            canonical = by_name.get(label.strip().casefold())
            if canonical is None:
                raise MemberCapabilityError(
                    f"Label '{label}' is not defined in {owner}/{repo}. "
                    f"Defined labels: {', '.join(defined) or '(none)'}. "
                    "Ask a human to add the label instead of introducing a new one."
                )
            if canonical not in resolved:
                resolved.append(canonical)
        return resolved

    async def linked_pull_requests(self, issue_url: str) -> list[str]:
        resource = self.parse_url(issue_url, expected_kind="issue")
        endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/timeline"
        )
        urls: list[str] = []
        async for event in self._iter_paginated_rest_items(
            endpoint, headers={"Accept": "application/vnd.github+json"}
        ):
            source = event.get("source", {})
            issue = source.get("issue", {}) if isinstance(source, dict) else {}
            if "pull_request" in issue and issue.get("html_url"):
                urls.append(str(issue["html_url"]))
        return list(dict.fromkeys(urls))

    # ------------------------------------------------------------------ #
    #   Pull requests                                                    #
    # ------------------------------------------------------------------ #

    async def create_pull_request(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        *,
        draft: bool,
        issue_url: str,
        closes_issue: bool,
    ) -> PullRequestCreated:
        owner, repo_name = self.parse_repo(repo)
        client = await self._get_client()
        base_branch = base.strip() or await self.default_branch(owner, repo_name)
        endpoint = f"/repos/{owner}/{repo_name}/pulls"
        params = {"head": f"{owner}:{head}", "base": base_branch, "state": "open"}
        existing_resp = await client.get(endpoint, params=params)
        _raise_for_status(existing_resp)
        existing = _as_list(existing_resp.json())
        if existing:
            pr = existing[0]
            created = False
        else:
            resp = await client.post(
                endpoint,
                json={
                    "title": title,
                    "head": head,
                    "base": base_branch,
                    "body": append_issue_link(body, issue_url, closes=closes_issue),
                    "draft": draft,
                },
            )
            _raise_for_status(resp)
            pr = resp.json()
            created = True
        resource = GitHubResource(owner, repo_name, int(pr.get("number") or 0), "pull")
        return PullRequestCreated(
            pr_number=resource.number,
            pr_url=str(pr.get("html_url") or ""),
            created=created,
            draft=bool(pr.get("draft", draft if created else False)),
            head=head,
            base=base_branch,
            target=WorkTarget.model_validate(_work_target(resource, pr)),
        )

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        drop_issue_links: bool,
    ) -> PullRequestUpdated:
        resource = self.parse_url(url, expected_kind="pull")
        payload: dict[str, Any] = {}
        if body is not None:
            if not drop_issue_links:
                pr = await self._pull_request(resource)
                body = preserve_issue_links(body, pr.get("body") or "")
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        client = await self._get_client()
        resp = await client.patch(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}",
            json=payload,
        )
        _raise_for_status(resp)
        pr = resp.json()
        response_body = pr.get("body")
        return PullRequestUpdated(
            pr_number=pr.get("number", resource.number),
            pr_url=pr.get("html_url", url),
            title=str(pr.get("title") or ""),
            body="" if response_body is None else response_body,
            target=WorkTarget.model_validate(_work_target(resource, pr, url)),
        )

    async def review(self, url: str, body: str, event: ReviewEvent) -> ReviewSubmitted:
        """Submit a review verdict on the PR head as a GitHub review.

        A review, unlike a conversation comment, is what GitHub counts: it
        consumes a pending review request and lists the member under
        ``reviewed-by``, so the patrol can follow the PR from then on.
        """
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        head_sha = self._pull_request_head_sha(resource, pr)
        client = await self._get_client()
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/reviews",
            json={
                "body": body.rstrip(),
                "event": _REVIEW_EVENTS[event],
                "commit_id": head_sha,
            },
        )
        _raise_for_status(resp)
        review = resp.json()
        return ReviewSubmitted(
            review_id=review.get("id"),
            html_url=review.get("html_url"),
            state=review.get("state"),
            commit_id=head_sha,
            submitted_at=review.get("submitted_at"),
            target=WorkTarget.model_validate(_work_target(resource, pr, url)),
        )

    async def review_comment(
        self,
        url: str,
        body: str,
        path: str,
        line: int,
        side: str,
        start_line: int | None,
        start_side: str | None,
    ) -> ReviewCommentWritten:
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        payload: dict[str, Any] = {
            "body": body.rstrip(),
            "commit_id": self._pull_request_head_sha(resource, pr),
            "path": path,
            "line": line,
            "side": side,
        }
        if start_line is not None and start_side is not None:
            payload["start_line"] = start_line
            payload["start_side"] = start_side
        client = await self._get_client()
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/comments",
            json=payload,
        )
        _raise_for_status(resp)
        comment = resp.json()
        return ReviewCommentWritten(
            review_comment_id=comment.get("id"),
            html_url=comment.get("html_url"),
            created_at=comment.get("created_at"),
            path=path,
            line=line,
            side=side,
            target=WorkTarget.model_validate(_work_target(resource, pr, url)),
        )

    async def reply(self, url: str, reply_target_id: int, body: str) -> ReplyWritten:
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        client = await self._get_client()
        response = await client.get(
            f"/repos/{resource.full_repo}/pulls/comments/{reply_target_id}"
        )
        _raise_for_status(response)
        root = response.json()
        expected_url = (
            f"{self.base_url}/repos/{resource.full_repo}/pulls/{resource.number}"
        )
        if (
            not isinstance(root, dict)
            or root.get("id") != reply_target_id
            or root.get("in_reply_to_id") is not None
            or root.get("pull_request_url") != expected_url
        ):
            raise MemberCapabilityError(
                f"Review comment '{reply_target_id}' is not replyable for this PR."
            )
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/comments/{reply_target_id}/replies",
            json={"body": body.rstrip()},
        )
        _raise_for_status(resp)
        reply = resp.json()
        return ReplyWritten(
            reply_comment_id=reply.get("id"),
            html_url=reply.get("html_url"),
            created_at=reply.get("created_at"),
            target=WorkTarget.model_validate(_work_target(resource, pr, url)),
        )

    async def add_reaction(
        self,
        repo: str,
        target: ReactionTarget,
        comment_id: int,
        reaction: str,
        pr_number: int | None = None,
    ) -> ReactionAdded:
        owner, repo_name = self.parse_repo(repo)
        client = await self._get_client()
        if target == "pr-review":
            if pr_number is None:
                raise MemberCapabilityError("A review reaction needs the PR number.")
            # A review's body is reached only through GraphQL.
            resp = await client.get(
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}/reviews/{comment_id}"
            )
            _raise_for_status(resp)
            await self._graphql(
                ADD_REACTION,
                {
                    "subject": str(resp.json()["node_id"]),
                    "content": _GRAPHQL_REACTIONS[reaction],
                },
            )
            return ReactionAdded(content=reaction, comment_id=comment_id)
        collection = "issues" if target == "issue-comment" else "pulls"
        resp = await client.post(
            f"/repos/{owner}/{repo_name}/{collection}/comments/{comment_id}/reactions",
            json={"content": reaction},
            headers={"Accept": "application/vnd.github+json"},
        )
        _raise_for_status(resp)
        payload = resp.json()
        return ReactionAdded(
            reaction_id=payload.get("id"),
            content=str(payload.get("content") or reaction),
            comment_id=comment_id,
        )

    async def open_pull_requests(self, remote_url: str, branch: str) -> list[str]:
        repository = self.repository_from_remote(remote_url)
        if repository is None:
            return []
        owner, repo = repository
        client = await self._get_client()
        # Member publishing targets branches in the configured origin repository;
        # fork-owned heads are outside this interactive push lookup.
        response = await client.get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}"},
        )
        _raise_for_status(response)
        return [
            str(
                pull_request.get("html_url")
                or f"{self.web_base_url()}/{owner}/{repo}/pull/{number}"
            )
            for pull_request in _as_list(response.json())
            if isinstance(number := pull_request.get("number"), int) and number >= 1
        ]

    @asynccontextmanager
    async def artifact(
        self, url: str, name: str, limit: int
    ) -> AsyncIterator[tuple[ArtifactRef, IO[bytes]]]:
        resource, run_id, head_sha = await self._artifact_target(url)
        actions = GitHubActionsClient(await self._get_client())
        try:
            artifacts = await actions.artifacts(
                resource.owner, resource.repo, name=name, run_id=run_id
            )
            candidates = [
                artifact
                for artifact in artifacts
                if artifact.get("name") == name
                and not artifact.get("expired", False)
                and (
                    head_sha is None
                    or (artifact.get("workflow_run") or {}).get("head_sha") == head_sha
                )
            ]
            if not candidates:
                raise MemberCapabilityError(
                    f"Artifact '{name}' was not found for {url}."
                )
            artifact = max(candidates, key=lambda item: int(item.get("id", 0)))
            artifact_id = int(artifact["id"])
            archive_size = int(artifact.get("size_in_bytes", 0))
            if archive_size > limit:
                raise MemberCapabilityError(
                    f"Artifact '{name}' is {archive_size} bytes, above the "
                    f"{limit} byte limit. Inspect the artifact from "
                    "the Actions run URL or ask a human to retrieve it."
                )
            ref = ArtifactRef(
                repo=resource.full_repo,
                run_id=run_id or (artifact.get("workflow_run") or {}).get("id"),
                artifact_id=artifact_id,
                artifact_name=name,
            )
            async with actions.artifact_archive(
                resource.owner, resource.repo, artifact_id, limit
            ) as archive:
                yield ref, archive
        except GitHubActionsClientError as exc:
            raise MemberCapabilityError(str(exc)) from exc

    async def _artifact_target(
        self, url: str
    ) -> tuple[GitHubResource, int | None, str | None]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= GITHUB_ACTIONS_RUN_MIN_PART_COUNT and parts[2:4] == [
            "actions",
            "runs",
        ]:
            try:
                run_id = int(parts[4])
            except ValueError as exc:
                raise MemberCapabilityError(
                    f"Unsupported GitHub Actions URL: {url}"
                ) from exc
            if run_id < 1:
                raise MemberCapabilityError(f"Unsupported GitHub Actions URL: {url}")
            return GitHubResource(parts[0], parts[1], run_id, "run"), run_id, None
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        return resource, None, self._pull_request_head_sha(resource, pr)

    # ------------------------------------------------------------------ #
    #   The git remote                                                   #
    # ------------------------------------------------------------------ #

    async def clone_url(self, owner: str, repo: str) -> str:
        return f"{self.web_base_url()}/{owner}/{repo}.git"

    async def default_branch(self, owner: str, repo: str) -> str:
        client = await self._get_client()
        resp = await client.get(f"/repos/{owner}/{repo}")
        _raise_for_status(resp)
        return str(resp.json().get("default_branch") or "main")

    async def pull_request_head(self, url: str) -> PullRequestHead:
        resource = self.parse_url(url, expected_kind="pull")
        return self._pull_request_head(resource, await self._pull_request(resource))

    async def push_credential(self) -> str:
        return await get_person_github_token(self.person, self.base_url)

    def commit_url(self, remote_url: str, sha: str) -> str:
        web_url = _remote_web_url(remote_url)
        if not web_url:
            return ""
        configured_host = urlparse(self.web_base_url()).hostname
        remote_host = urlparse(web_url).hostname
        if configured_host != remote_host:
            return ""
        return f"{web_url}/commit/{sha}"

    def remote_host(self, remote_url: str) -> str:
        return urlparse(_remote_web_url(remote_url)).hostname or "unrecognized remote"

    def repository_from_remote(self, remote_url: str) -> tuple[str, str] | None:
        web_url = _remote_web_url(remote_url)
        if not web_url:
            return None
        parsed = urlparse(web_url)
        if parsed.hostname != urlparse(self.web_base_url()).hostname:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != REPO_WITH_OWNER_PART_COUNT:
            return None
        return parts[0], parts[1]

    # ------------------------------------------------------------------ #
    #   Pull request patrol                                              #
    # ------------------------------------------------------------------ #

    async def pull_request_candidates(self) -> list[Task]:
        """Every open PR that asks something of the member, oldest update first."""
        candidates: list[Task] = []
        for item in await self._search_pull_requests():
            owner, _, repo = (
                str(item.get("repository_url") or "")
                .rpartition("/repos/")[2]
                .partition("/")
            )
            task = await self._pull_request_work(owner, repo, int(item["number"]))
            if task is not None:
                candidates.append(task)
        return candidates

    async def refresh_pull_request(self, task: Task) -> Task | None:
        if task.number is None or not task.repository:
            return None
        return await self._pull_request_work(self.owner, task.repository, task.number)

    async def _pull_request_work(
        self, owner: str, repo: str, number: int
    ) -> Task | None:
        """What the PR asks of the member now; one at the re-review limit is
        handed over instead."""
        data = await self._graphql(
            PULL_REQUEST_QUERY, {"owner": owner, "repo": repo, "number": number}
        )
        node = (data.get("repository") or {}).get("pullRequest")
        if not node:
            raise RuntimeError(f"Pull request unavailable: {owner}/{repo}#{number}")
        pull_request = parse_pull_request(node, repo)
        work = pull_request_work(pull_request, self._login())
        if work is None:
            return None
        task = self._pull_request_task(pull_request, work)
        if work == REVIEW_LIMIT:
            await self._hand_over_at_review_limit(task)
            return None
        return task

    async def _search_pull_requests(self) -> list[dict[str, Any]]:
        """Open PRs under the project owner that name this member.

        Search is the one listing that needs no repository list and works for
        both PAT and GitHub App logins (``<app>[bot]``). Three qualifiers cover
        the two roles: written by, reviewed by, and review requested from the
        member (the last never matches a GitHub App, which GitHub cannot
        request a review from).

        Draft PRs are left out at the search: a draft is the human's "hands
        off" switch (the patrol flips it only to hand a PR over at the review
        limit), so neither role acts on it until someone marks the PR ready for
        review.
        """
        client = await self._get_client()
        username = get_github_username(self.person, strict=True)
        found: dict[str, dict[str, Any]] = {}
        for qualifier in ("author", "reviewed-by", "review-requested"):
            resp = await client.get(
                "/search/issues",
                params={
                    "q": (
                        f"is:pr is:open draft:false user:{self.owner} "
                        f"{qualifier}:{username}"
                    ),
                    "per_page": 100,
                    "sort": "updated",
                    "order": "asc",
                },
            )
            if resp.status_code >= HTTPStatus.BAD_REQUEST:
                raise RuntimeError(
                    f"Pull request search failed ({resp.status_code}): {resp.text}"
                )
            for item in resp.json().get("items") or []:
                found.setdefault(str(item.get("html_url") or ""), item)
        return sorted(
            found.values(), key=lambda item: str(item.get("updated_at") or "")
        )

    def _login(self) -> str:
        return normalize_login(get_github_username(self.person, strict=True))

    def _pull_request_task(
        self, pull_request: PullRequest, trigger_reason: str
    ) -> Task:
        return Task(
            id=pull_request.node_id,
            number=pull_request.number,
            url=pull_request.url,
            title=pull_request.title,
            description=pull_request.body,
            status=Task.IN_PROGRESS,
            created_at=parse_timestamp(pull_request.created_at),
            repository=pull_request.repository,
            assignee=self.person.person_id,
            pull_request_url=pull_request.url,
            trigger_reason=trigger_reason,
        )

    async def _hand_over_at_review_limit(self, task: Task) -> None:
        """Make the PR a draft, then say on it that automatic re-review stopped.

        The notice tells the human that the PR is theirs until they mark it
        ready for review, so it is posted only once the PR is a draft. When
        the conversion fails, a failure notice takes its place: it holds the
        PR until someone acts on it, and the rounds stay as they are.
        """
        try:
            await self._graphql(CONVERT_PULL_REQUEST_TO_DRAFT, {"pullRequest": task.id})
        except Exception as exc:
            _LOGGER.warning(
                f"Could not convert {task.pull_request_url} to a draft: {exc}"
            )
            reason = "failed"
            body = t(
                "integrations.github.github_ticket_manager.review_limit_draft_failed",
                count=MAX_REVIEW_ROUNDS,
            )
        else:
            reason = REVIEW_LIMIT_REASON
            body = t(
                "integrations.github.github_ticket_manager.review_limit_reached",
                count=MAX_REVIEW_ROUNDS,
            )
        assert task.pull_request_url
        await self.comment(
            task.pull_request_url,
            body,
            status=workflow_status_fields(
                reason=reason,
                person_id=self.person.person_id,
                run_id="",
                subject_id=task.pull_request_url,
            ),
        )

    # ------------------------------------------------------------------ #
    #   Requests                                                         #
    # ------------------------------------------------------------------ #

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        _raise_for_status(resp)
        payload = resp.json()
        if payload.get("errors"):
            raise MemberCapabilityError(str(payload["errors"]))
        return payload["data"]

    async def _paginated_rest_items(
        self, endpoint: str, *, headers: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        return [
            item
            async for item in self._iter_paginated_rest_items(endpoint, headers=headers)
        ]

    async def _iter_paginated_rest_items(
        self, endpoint: str, *, headers: dict[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        client = await self._get_client()

        def parse_page(response: Any) -> list[dict[str, Any]]:
            _raise_for_status(response)
            return _as_list(response.json())

        async for item in paginated_items(
            client.get,
            endpoint,
            parse_page,
            headers=headers,
        ):
            yield item

    async def _post_comment(self, endpoint: str, body: str) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.post(endpoint, json={"body": body.rstrip()})
        _raise_for_status(resp)
        return resp.json()

    def parse_repo(self, repo: str) -> tuple[str, str]:
        parts = [part for part in repo.strip().split("/") if part]
        if len(parts) == 1 and self.owner:
            return self.owner, parts[0]
        if len(parts) == REPO_WITH_OWNER_PART_COUNT:
            return parts[0], parts[1]
        raise MemberCapabilityError(
            f"Repository must be '<owner>/<repo>' or '<repo>': {repo}"
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
    except (ValueError, binascii.Error, UnicodeError):
        raise RepositoryReadError(t("integrations.repository.continuation")) from None


def _continuation(response: httpx.Response, request: dict[str, Any]) -> str | None:
    link = response.links.get("next")
    if link is None:
        return None
    try:
        # GitHub may canonicalize the URL to /repositories/{id}/..., or omit
        # filters. Only the cursor is data we need; we never request this URL.
        cursors = httpx.URL(link["url"]).params.get_list("after")
        if len(cursors) != 1 or not 0 < len(cursors[0]) <= _CURSOR_LIMIT:
            raise ValueError
    except (KeyError, ValueError, httpx.InvalidURL):
        raise RepositoryReadError(t("integrations.repository.continuation")) from None
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": cursors[0]}).encode()
    ).decode()


_PERMISSIONS = {
    "dependency_alerts": "Dependabot alerts",
    "issues": "Issues",
    "issue_comments": "Issues / Pull requests",
    "issue_timeline": "Issues / Pull requests",
    "issue_projects": "Projects",
    "pull_requests": "Pull requests",
    "pull_request_reviews": "Pull requests",
    "pull_request_files": "Pull requests",
    "pull_request_threads": "Pull requests",
    "review_thread_comments": "Pull requests",
    "pull_request_readiness": "Pull requests / Contents / Checks / Commit statuses / Actions",
}


def _access_message(kind: str, resource: str) -> str:
    message = (
        t("integrations.github.read.forbidden", permission=_PERMISSIONS[resource])
        if kind == "forbidden"
        else t("integrations.github.read.not_found", permission=_PERMISSIONS[resource])
    )
    if kind == "not_found" and resource == "dependency_alerts":
        message += " " + t("integrations.github.read.alerts_disabled")
    return message


def _graphql_error(errors: Any, resource: str) -> RepositoryReadError:
    kinds = {item.get("type") for item in errors if isinstance(item, dict)}
    if "RATE_LIMITED" in kinds:
        return RepositoryReadError(t("integrations.github.read.rate_limit"))
    if kinds & {"FORBIDDEN", "INSUFFICIENT_SCOPES"}:
        return RepositoryReadError(_access_message("forbidden", resource))
    if "NOT_FOUND" in kinds:
        return RepositoryReadError(_access_message("not_found", resource))
    return RepositoryReadError(t("integrations.github.read.response"))


def _http_error(response: httpx.Response, resource: str) -> RepositoryReadError:
    status = response.status_code
    if status == HTTPStatus.TOO_MANY_REQUESTS or (
        status == HTTPStatus.FORBIDDEN
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    ):
        return RepositoryReadError(t("integrations.github.read.rate_limit"))
    message = {
        401: t("integrations.github.read.authentication"),
        403: _access_message("forbidden", resource),
        404: _access_message("not_found", resource),
        422: t("integrations.github.read.rejected"),
    }.get(status, t("integrations.github.read.http", status=status))
    return RepositoryReadError(message)


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


def _encode_cursor(cursor: str, request: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": cursor}).encode()
    ).decode()


def _alert_query(conditions: dict[str, Any]) -> dict[str, Any]:
    """A page of dependency alerts as GitHub filters them."""
    return {
        "state": {
            "open": "open",
            "resolved": "fixed",
            "dismissed": "dismissed,auto_dismissed",
        }[conditions["state"]],
        "per_page": conditions["page_size"],
    }


def _author_type(person: Any, item: dict[str, Any]) -> str:
    login = str((item.get("user") or {}).get("login") or "")
    return get_author_type(person, login) if login else ""


def _item_ref(web_base_url: str, resource: GitHubResource) -> ItemRef:
    kind: ItemKind = "pull_request" if resource.kind == "pull" else "issue"
    collection = "pull" if resource.kind == "pull" else "issues"
    return ItemRef(
        kind=kind,
        owner=resource.owner,
        repo=resource.repo,
        number=resource.number,
        url=f"{web_base_url}/{resource.full_repo}/{collection}/{resource.number}",
    )


def _addressed(comment: str, author: str, person: Any) -> str:
    """``comment`` with a mention of the item's author, unless the member is
    the author or the comment already mentions them (logins are
    case-insensitive)."""
    if not author or normalize_login(author) == normalize_login(
        get_github_username(person)
    ):
        return comment
    mention = r"(^|[^A-Za-z0-9_])@" + re.escape(author) + r"(?=$|[^A-Za-z0-9-])"
    if re.search(mention, comment, flags=re.IGNORECASE):
        return comment
    return f"@{author}\n\n{comment}"


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(item.get("name", "")) for item in _as_list(issue.get("labels"))]


def _remote_web_url(remote_url: str) -> str:
    value = remote_url.strip()
    if not value:
        return ""
    if "://" not in value and ":" in value:
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        return _strip_git_suffix(f"https://{host}/{path}") if host and path else ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path:
        return _strip_git_suffix(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return _strip_git_suffix(f"https://{parsed.hostname}{parsed.path}")
    return ""


def _strip_git_suffix(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")
