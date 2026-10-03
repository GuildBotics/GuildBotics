from __future__ import annotations

import re
import tempfile
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from shutil import copyfileobj
from typing import IO, Any
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt
from pydantic import TypeAdapter, ValidationError

from guildbotics.capabilities.artifact_archive import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    Unpacked,
    extract_artifact,
    unpacked,
)
from guildbotics.capabilities.chat_updates import ensure_chat_current
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.actions_client import (
    GitHubActionsClient,
    GitHubActionsClientError,
)
from guildbotics.integrations.github.github_utils import (
    get_github_username,
    normalize_login,
    paginated_items,
)
from guildbotics.integrations.github.pull_requests import (
    GITHUB_ACTIONS_RUN_MIN_PART_COUNT,
    GitHubPullRequestHead,
    GitHubPullRequests,
    GitHubResource,
    MemberCapabilityError,
    _pull_request_readiness_applies,
    _raise_for_status,
    _work_target,
)
from guildbotics.integrations.github.repository_scope import (
    ADD_PROJECT_ITEM,
)
from guildbotics.runtime.member_invocation import (
    GuestProcessError,
    current_member_invocation,
)
from guildbotics.utils.person_profile import build_member_communication_style
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

REPO_WITH_OWNER_PART_COUNT = 2
_REVIEW_EVENTS = {
    "approve": "APPROVE",
    "request-changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


class MemberGitHubCapabilityService(GitHubPullRequests):
    def __init__(self, person: Person, team: Team) -> None:
        super().__init__(person, team)
        ticket_config = team.project.get_service_config(Service.TICKET_MANAGER)
        self.project_owner = str(ticket_config.get("owner") or self.owner)
        self.project_id = str(ticket_config.get("project_id") or "")
        self.project_url = str(ticket_config.get("url") or "")
        self._project_node_id: str | None = None

    async def context(self, check_credentials: bool = False) -> dict[str, Any]:
        credential_status = "unchecked"
        if check_credentials:
            client = await self._get_client()
            # /rate_limit is readable by every credential type (PAT, machine
            # user, and GitHub App installation token). /user would 403 for
            # installation tokens (github_apps members), so it cannot be used
            # as a generic credential probe.
            resp = await client.get("/rate_limit")
            _raise_for_status(resp)
            credential_status = "ok"
        role_summaries = {
            role_id: {
                "summary": role.summary,
                "description": role.description,
            }
            for role_id, role in self.person.roles.items()
        }
        return {
            "person_id": self.person.person_id,
            "name": self.person.name,
            "person_type": self.person.person_type,
            "is_active": self.person.is_active,
            "roles": role_summaries,
            "profile": self.person.profile,
            "speaking_style": self.person.speaking_style,
            "communication_style": build_member_communication_style(self.person),
            "github_username": get_github_username(self.person),
            "credential_status": credential_status,
            "memory": MemberMemoryService(self.person).load_context_memory(),
            # The full member command surface and cross-cutting rules. This is
            # the same reference printed by ``guildbotics member help`` and is
            # the single source every entrypoint relies on (context is the
            # mandatory first call), so each member can perform GitHub, git, and
            # chat work regardless of which workflow invoked it. Task contracts
            # (primary objective, required completion command) stay in the
            # prompts, never here.
            "capabilities": capability_reference_text(),
        }

    async def issue_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
        # The target is read before the write, never after it: a read that
        # fails after the comment landed would make a retry post it twice. The
        # freshness check runs first so a refused write costs no read either.
        ensure_chat_current(self.person.person_id)
        issue = await self._issue(resource)
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        result = _comment_result(comment)
        result.update(
            {
                "comment_url": result["html_url"],
                "issue_number": resource.number,
                "repo": resource.full_repo,
                "issue_url": (
                    f"{self.web_base_url()}/{resource.full_repo}/issues/{resource.number}"
                ),
                "target": _work_target(resource, issue, url),
            }
        )
        return result

    async def issue_create(
        self,
        repo: str,
        title: str,
        body: str,
        add_to_project: bool,
        labels: Sequence[str] = (),
        human_approved: bool = False,
    ) -> dict[str, Any]:
        _require_human_approval(human_approved, "Creating an issue")
        owner, repo_name = self.parse_repo(repo)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = await self._defined_labels(owner, repo_name, labels)
        client = await self._get_client()
        ensure_chat_current(self.person.person_id)
        resp = await client.post(f"/repos/{owner}/{repo_name}/issues", json=payload)
        _raise_for_status(resp)
        issue = resp.json()
        project_item_id = None
        if add_to_project and issue.get("node_id"):
            project_item_id = await self.add_project_item(str(issue["node_id"]))
        resource = GitHubResource(
            owner, repo_name, int(issue.get("number") or 0), "issue"
        )
        issue_url = (
            issue.get("html_url")
            or f"{self.web_base_url()}/{owner}/{repo_name}/issues/{resource.number}"
        )
        return {
            "issue_number": issue.get("number"),
            "issue_title": issue.get("title", title),
            "repo": f"{owner}/{repo_name}",
            "issue_url": issue_url,
            "labels": _label_names(issue),
            "project_item_id": project_item_id,
            "target": _work_target(resource, issue, issue_url),
        }

    async def issue_update(
        self,
        url: str,
        body: str | None = None,
        title: str | None = None,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
        state: str | None = None,
        state_reason: str | None = None,
        human_approved: bool = False,
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
        if state is not None:
            _require_human_approval(
                human_approved, f"Changing the issue state to '{state}'"
            )
        add_labels = _cleaned_labels(add_labels)
        remove_labels = _cleaned_labels(remove_labels)
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
            if state_reason is not None:
                payload["state_reason"] = state_reason
        if not payload and not (add_labels or remove_labels):
            raise MemberCapabilityError(
                "issue update needs at least one of body, title, labels, or state."
            )
        # Label additions are validated up front so an undefined label aborts
        # before any write; the label endpoints are called only after the field
        # PATCH succeeded so a failed PATCH leaves the labels untouched.
        additions = await self._defined_labels(
            resource.owner, resource.repo, add_labels
        )
        state_changed = False
        if state is not None:
            state_changed = await self._issue_state(resource) != state
        client = await self._get_client()
        issue_endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}"
        )
        if payload:
            ensure_chat_current(self.person.person_id)
            resp = await client.patch(issue_endpoint, json=payload)
            _raise_for_status(resp)
        await self._apply_label_changes(resource, additions, remove_labels)
        if additions or remove_labels or not payload:
            resp = await client.get(issue_endpoint)
            _raise_for_status(resp)
        issue = resp.json()
        response_body = issue.get("body")
        return {
            "issue_number": issue.get("number", resource.number),
            "issue_url": issue.get("html_url", url),
            "repo": resource.full_repo,
            "title": issue.get("title", ""),
            "state": issue.get("state", ""),
            "state_changed": state_changed,
            "labels": _label_names(issue),
            "body": "" if response_body is None else response_body,
            "target": _work_target(resource, issue, url),
        }

    async def _issue_state(self, resource: GitHubResource) -> str:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}"
        )
        _raise_for_status(resp)
        return str(resp.json().get("state", ""))

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
            ensure_chat_current(self.person.person_id)
            resp = await client.delete(f"{labels_endpoint}/{quote(name, safe='')}")
            if resp.status_code != HTTPStatus.NOT_FOUND:
                _raise_for_status(resp)
        if additions:
            ensure_chat_current(self.person.person_id)
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
        defined = await self._repository_labels(owner, repo)
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

    async def _repository_labels(self, owner: str, repo: str) -> list[str]:
        items = await self._paginated_rest_items(f"/repos/{owner}/{repo}/labels")
        names = [str(item.get("name", "")) for item in items]
        return [name for name in names if name]

    async def open_pr_checks(
        self, remote_url: str, branch: str
    ) -> list[dict[str, Any]]:
        """Return readiness for open PRs whose head is the pushed branch."""
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
        results: list[dict[str, Any]] = []
        for pull_request in _as_list(response.json()):
            number = pull_request.get("number")
            if not isinstance(number, int) or number < 1:
                continue
            url = str(
                pull_request.get("html_url")
                or f"{self.web_base_url()}/{owner}/{repo}/pull/{number}"
            )
            checks = await self.pr_checks(url)
            if checks["readiness"] == "not_applicable":
                continue
            results.append(
                {
                    "pr_url": checks["pr_url"],
                    "readiness": checks["readiness"],
                    "completion_blockers": checks["completion_blockers"],
                }
            )
        return results

    async def _linked_pull_request_urls(self, resource: GitHubResource) -> list[str]:
        endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/timeline"
        )
        events: list[dict[str, Any]] = []
        async for event in self._iter_paginated_rest_items(
            endpoint, headers={"Accept": "application/vnd.github+json"}
        ):
            events.append(event)

        urls: list[str] = []
        for event in events:
            source = event.get("source", {})
            issue = source.get("issue", {}) if isinstance(source, dict) else {}
            if "pull_request" in issue and issue.get("html_url"):
                urls.append(str(issue["html_url"]))
        return list(dict.fromkeys(urls))

    async def task_completion_readiness(
        self, ticket_url: str, evidence: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Revalidate every PR a ticket run is answerable for before completion.

        PRs the run pushed to or opened (evidence) are always targets. The
        ticket itself, when it is a PR, and PRs linked from an issue ticket
        are targets only when the member authored them: a reviewer cannot
        make someone else's PR ready.
        """
        touched = self._task_pull_request_urls(evidence)
        try:
            ticket = self.parse_url(ticket_url)
        except (MemberCapabilityError, ValueError):
            ticket = None
        urls = [ticket_url] if ticket is not None and ticket.kind == "pull" else []
        urls.extend(touched)
        if ticket is not None and ticket.kind == "issue":
            urls.extend(await self._linked_pull_request_urls(ticket))
        results = []
        for url in dict.fromkeys(urls):
            resource = self.parse_url(url, expected_kind="pull")
            pr = await self._pull_request(resource)
            if not _pull_request_readiness_applies(pr) or (
                url not in touched and not self._authored(pr)
            ):
                continue
            result = await self.pr_checks(url)
            if result["readiness"] != "not_applicable":
                results.append(result)
        blocked = [result for result in results if result["readiness"] != "ready"]
        if blocked:
            details = "; ".join(
                f"{result['pr_url']}: "
                + ", ".join(
                    str(blocker["message"]) for blocker in result["completion_blockers"]
                )
                for result in blocked
            )
            raise MemberCapabilityError(
                "Task cannot be completed because PR readiness is blocked. " + details
            )
        return results

    async def artifact_download(
        self, url: str, name: str, destination: Path
    ) -> dict[str, Any]:
        """Download and safely extract one Actions artifact."""
        name = name.strip()
        if not name:
            raise MemberCapabilityError("Artifact name is required.")
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
            if archive_size > MAX_ARTIFACT_BYTES:
                raise MemberCapabilityError(
                    f"Artifact '{name}' is {archive_size} bytes, above the "
                    f"{MAX_ARTIFACT_BYTES} byte limit. Inspect the artifact from "
                    "the Actions run URL or ask a human to retrieve it."
                )
            async with actions.artifact_archive(
                resource.owner,
                resource.repo,
                artifact_id,
                MAX_ARTIFACT_BYTES,
            ) as archive:
                where = _unpack(archive, destination)
        except GitHubActionsClientError as exc:
            raise MemberCapabilityError(str(exc)) from exc
        return {
            "repo": resource.full_repo,
            "run_id": run_id or (artifact.get("workflow_run") or {}).get("id"),
            "artifact_id": artifact_id,
            "artifact_name": name,
            **where,
        }

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

    async def pr_create(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        issue_url: str,
        draft: str,
        closes_issue: bool = False,
    ) -> dict[str, Any]:
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
            return {
                "pr_number": pr.get("number"),
                "pr_url": pr.get("html_url"),
                "created": False,
                "draft": bool(pr.get("draft", False)),
                "head": head,
                "base": base_branch,
                "target": _work_target(_pull_resource(owner, repo_name, pr), pr),
            }

        body = _append_issue_link(body, issue_url, closes=closes_issue)
        payload: dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base_branch,
            "body": body,
            "draft": draft == "true",
        }
        ensure_chat_current(self.person.person_id)
        resp = await client.post(endpoint, json=payload)
        _raise_for_status(resp)
        pr = resp.json()
        return {
            "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url"),
            "created": True,
            "draft": bool(pr.get("draft", payload.get("draft", False))),
            "head": head,
            "base": base_branch,
            "target": _work_target(_pull_resource(owner, repo_name, pr), pr),
        }

    async def pr_update(
        self,
        url: str,
        body: str | None = None,
        title: str | None = None,
        *,
        drop_issue_links: bool = False,
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        if drop_issue_links and body is None:
            raise MemberCapabilityError("--drop-issue-links requires a body.")
        payload: dict[str, Any] = {}
        if body is not None:
            if not drop_issue_links:
                pr = await self._pull_request(resource)
                body = _preserve_issue_links(body, pr.get("body") or "")
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if not payload:
            raise MemberCapabilityError("pr update needs a body or a title.")
        client = await self._get_client()
        ensure_chat_current(self.person.person_id)
        resp = await client.patch(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}",
            json=payload,
        )
        _raise_for_status(resp)
        pr = resp.json()
        response_body = pr.get("body")
        return {
            "pr_number": pr.get("number", resource.number),
            "pr_url": pr.get("html_url", url),
            "title": pr.get("title", ""),
            "body": "" if response_body is None else response_body,
            "target": _work_target(resource, pr, url),
        }

    async def pr_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        ensure_chat_current(self.person.person_id)
        pr = await self._pull_request(resource)
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        return {**_comment_result(comment), "target": _work_target(resource, pr, url)}

    async def pr_review(self, url: str, body: str, event: str) -> dict[str, Any]:
        """Submit a review verdict on the PR head as a GitHub review.

        A review, unlike a conversation comment, is what GitHub counts: it
        consumes a pending review request and lists the member under
        ``reviewed-by``, so the patrol can follow the PR from then on.
        """
        if event not in _REVIEW_EVENTS:
            raise MemberCapabilityError(
                "Review event must be one of " + ", ".join(sorted(_REVIEW_EVENTS)) + "."
            )
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        head_sha = self._pull_request_head_sha(resource, pr)
        client = await self._get_client()
        ensure_chat_current(self.person.person_id)
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
        return {
            "review_id": review.get("id"),
            "html_url": review.get("html_url"),
            "state": review.get("state"),
            "commit_id": head_sha,
            "submitted_at": review.get("submitted_at"),
            "target": _work_target(resource, pr, url),
        }

    async def pr_review_comment(
        self,
        url: str,
        body: str,
        path: str,
        line: int,
        side: str,
        start_line: int | None,
        start_side: str | None,
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        self._validate_review_comment_location(path, line, side, start_line, start_side)
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
        ensure_chat_current(self.person.person_id)
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/comments",
            json=payload,
        )
        _raise_for_status(resp)
        comment = resp.json()
        return {
            "review_comment_id": comment.get("id"),
            "html_url": comment.get("html_url"),
            "created_at": comment.get("created_at"),
            "path": path,
            "line": line,
            "side": side,
            "target": _work_target(resource, pr, url),
        }

    async def pr_reply(
        self, url: str, reply_target_id: int, body: str
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        ensure_chat_current(self.person.person_id)
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
        client = await self._get_client()
        ensure_chat_current(self.person.person_id)
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/comments/{reply_target_id}/replies",
            json={"body": body.rstrip()},
        )
        _raise_for_status(resp)
        reply = resp.json()
        return {
            "reply_comment_id": reply.get("id"),
            "html_url": reply.get("html_url"),
            "created_at": reply.get("created_at"),
            "target": _work_target(resource, pr, url),
        }

    async def reaction_add(
        self, repo: str, target: str, comment_id: int, reaction: str
    ) -> dict[str, Any]:
        owner, repo_name = self.parse_repo(repo)
        if target == "issue-comment":
            endpoint = (
                f"/repos/{owner}/{repo_name}/issues/comments/{comment_id}/reactions"
            )
        elif target == "pr-review-comment":
            endpoint = (
                f"/repos/{owner}/{repo_name}/pulls/comments/{comment_id}/reactions"
            )
        else:
            raise MemberCapabilityError(f"Unsupported reaction target '{target}'.")
        client = await self._get_client()
        ensure_chat_current(self.person.person_id)
        resp = await client.post(
            endpoint,
            json={"content": reaction},
            headers={"Accept": "application/vnd.github+json"},
        )
        _raise_for_status(resp)
        payload = resp.json()
        return {
            "reaction_id": payload.get("id"),
            "content": payload.get("content", reaction),
            "comment_id": comment_id,
        }

    async def default_branch(self, owner: str, repo: str) -> str:
        client = await self._get_client()
        resp = await client.get(f"/repos/{owner}/{repo}")
        _raise_for_status(resp)
        return str(resp.json().get("default_branch") or "main")

    async def get_clone_url(self, owner: str, repo: str) -> str:
        return f"{self.web_base_url()}/{owner}/{repo}.git"

    def commit_url_from_remote(self, remote_url: str, sha: str) -> str:
        web_url = _remote_web_url(remote_url)
        if not web_url:
            return ""
        configured_host = urlparse(self.web_base_url()).hostname
        remote_host = urlparse(web_url).hostname
        if configured_host != remote_host:
            return ""
        return f"{web_url}/commit/{sha}"

    def remote_host(self, remote_url: str) -> str:
        """The host a remote names, without a credential its URL may carry."""
        return urlparse(_remote_web_url(remote_url)).hostname or "unrecognized remote"

    def repository_from_remote(self, remote_url: str) -> tuple[str, str] | None:
        """The ``(owner, repo)`` a remote names on the configured code host."""
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

    def _authored(self, pr: dict[str, Any]) -> bool:
        login = str((pr.get("user") or {}).get("login") or "")
        return normalize_login(login) == normalize_login(
            get_github_username(self.person)
        )

    def _task_pull_request_urls(self, evidence: list[dict[str, Any]]) -> list[str]:
        candidates: list[str] = []
        for record in evidence:
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            for key in ("pr_url", "html_url"):
                value = payload.get(key)
                if isinstance(value, str):
                    candidates.append(value)
            pull_requests = payload.get("pull_requests")
            if isinstance(pull_requests, list):
                candidates.extend(
                    str(item.get("pr_url") or "")
                    for item in pull_requests
                    if isinstance(item, dict)
                )

        urls: list[str] = []
        for candidate in candidates:
            try:
                resource = self.parse_url(candidate, expected_kind="pull")
            except (MemberCapabilityError, ValueError):
                continue
            canonical = (
                f"{self.web_base_url()}/{resource.full_repo}/pull/{resource.number}"
            )
            if canonical not in urls:
                urls.append(canonical)
        return urls

    async def get_pr_head(self, url: str) -> GitHubPullRequestHead:
        resource = self.parse_url(url, expected_kind="pull")
        return self._pull_request_head(resource, await self._pull_request(resource))

    def _validate_review_comment_location(
        self,
        path: str,
        line: int,
        side: str,
        start_line: int | None,
        start_side: str | None,
    ) -> None:
        if not path.strip():
            raise MemberCapabilityError("Review comment path is required.")
        if line < 1:
            raise MemberCapabilityError("Review comment line must be positive.")
        if side not in {"LEFT", "RIGHT"}:
            raise MemberCapabilityError("Review comment side must be LEFT or RIGHT.")
        if start_side is not None and start_side not in {"LEFT", "RIGHT"}:
            raise MemberCapabilityError(
                "Review comment start_side must be LEFT or RIGHT."
            )
        if (start_line is None) != (start_side is None):
            raise MemberCapabilityError(
                "Review comment range requires both start_line and start_side."
            )
        if start_line is not None and start_line < 1:
            raise MemberCapabilityError("Review comment start_line must be positive.")
        if start_side is not None and start_side != side:
            raise MemberCapabilityError(
                "Review comment range start_side must match side."
            )
        if start_line is not None and start_line > line:
            raise MemberCapabilityError(
                "Review comment range start_line must be less than or equal to line."
            )

    def parse_repo(self, repo: str) -> tuple[str, str]:
        parts = [part for part in repo.strip().split("/") if part]
        if len(parts) == 1 and self.owner:
            return self.owner, parts[0]
        if len(parts) == REPO_WITH_OWNER_PART_COUNT:
            return parts[0], parts[1]
        raise MemberCapabilityError(
            f"Repository must be '<owner>/<repo>' or '<repo>': {repo}"
        )

    async def add_project_item(self, issue_node_id: str) -> str | None:
        if not self.project_id:
            return None
        project_node_id = await self._project_node()
        ensure_chat_current(self.person.person_id)
        data = await self._graphql(
            ADD_PROJECT_ITEM, {"proj": project_node_id, "content": issue_node_id}
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    async def _project_node(self) -> str:
        if self._project_node_id:
            return self._project_node_id
        if not self.project_owner or not self.project_id:
            raise MemberCapabilityError("GitHub ProjectV2 is not configured.")
        query_type = "organization" if "/orgs/" in self.project_url else "user"
        query = f"""
        query($owner:String!, $num:Int!) {{
          {query_type}(login:$owner) {{
            projectV2(number:$num) {{ id }}
          }}
        }}
        """
        data = await self._graphql(
            query, {"owner": self.project_owner, "num": int(self.project_id)}
        )
        project = data[query_type]["projectV2"]
        if not project:
            raise MemberCapabilityError(
                f"ProjectV2 number={self.project_id} not found for {self.project_owner}."
            )
        self._project_node_id = str(project["id"])
        return self._project_node_id

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
        ensure_chat_current(self.person.person_id)
        resp = await client.post(endpoint, json={"body": body.rstrip()})
        _raise_for_status(resp)
        return resp.json()


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(item.get("name", "")) for item in _as_list(issue.get("labels"))]


def _cleaned_labels(labels: Sequence[str]) -> list[str]:
    return [name for name in (label.strip() for label in labels) if name]


def _require_human_approval(human_approved: bool, action: str) -> None:
    """Guard the writes that stay a human decision.

    Opening and closing issues shape the team's backlog, so the member may only
    perform them on a human's instruction or approval. The flag is the member's
    attestation that such an instruction exists in the originating conversation.
    """
    if not human_approved:
        raise MemberCapabilityError(
            f"{action} requires a human instruction or approval. "
            "Ask the human first, then re-run with --human-approved."
        )


def _pull_resource(owner: str, repo: str, pr: dict[str, Any]) -> GitHubResource:
    return GitHubResource(owner, repo, int(pr.get("number") or 0), "pull")


def _comment_result(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "comment_id": comment.get("id"),
        "html_url": comment.get("html_url"),
        "author": user.get("login", ""),
        "created_at": comment.get("created_at"),
    }


_UNPACKED = TypeAdapter(Unpacked)


def _unpack(archive: IO[bytes], destination: Path) -> Unpacked:
    """Unpack ``archive`` where the command that asked for it reads it: on
    the host, or, for a command of an isolated environment, in that
    environment (:mod:`.artifact_archive`), which the host writes nothing of.

    Raises:
        MemberCapabilityError: When the artifact cannot be unpacked safely,
            or the environment could not unpack it.
    """
    guest = current_member_invocation().guest
    if guest is None:
        try:
            return unpacked(destination, extract_artifact(archive, destination))
        except ArtifactError as exc:
            raise MemberCapabilityError(str(exc)) from exc
    # A process that ran out of time may still hold the copy open (on Windows
    # it cannot be deleted then); the failure is what the command reports.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as held:
        copy = Path(held) / "artifact.zip"
        with copy.open("wb") as file:
            copyfileobj(archive, file)
        try:
            result = guest.run(
                guest.python(
                    "guildbotics.capabilities.artifact_archive",
                    guest.path(destination),
                ),
                cwd="/",
                env={},
                stdin=copy,
                stdout_limit=STREAM_READ_LIMIT,
            )
        except GuestProcessError as exc:
            raise MemberCapabilityError(str(exc)) from exc
    if result.returncode != 0:
        raise MemberCapabilityError(
            result.stderr.decode(errors="replace").strip()
            or "The artifact could not be unpacked."
        )
    # What the environment wrote is its own to write: read as nothing more
    # than where it says it unpacked.
    try:
        return _UNPACKED.validate_json(result.stdout)
    except ValidationError as exc:
        raise MemberCapabilityError(
            "The environment reported the artifact unpacked in a form it cannot"
            " have been."
        ) from exc


_ISSUE_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)"
_ISSUE_REFS_KEYWORD = r"refs?"
_ISSUE_LINK = re.compile(
    rf"((?:{_ISSUE_CLOSING_KEYWORD}|{_ISSUE_REFS_KEYWORD})[ \t]+#(\d+))",
    re.I,
)
_ISSUE_SOURCE_LINK = re.compile(
    rf"(?:[ \t]*(?:[-+*]|\d+[.)])[ \t]+)*[ \t]*{_ISSUE_LINK.pattern}[ \t]*", re.I
)


@dataclass(frozen=True)
class _IssueLink:
    start: int
    end: int
    trailer: str
    number: str


def _issue_links(body: str) -> Iterator[_IssueLink]:
    """Select standalone paragraph or list links, excluding quoted examples."""
    source = body.splitlines(keepends=True)
    offsets = [0]
    for line in source:
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").parse(body)
    quoted = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        if (
            token.type != "inline"
            or tokens[index - 1].type != "paragraph_open"
            or token.map is None
            or quoted
        ):
            continue
        code_rows: set[int] = set()
        code_contents = {
            child.content
            for child in token.children or []
            if child.type == "code_inline"
        }
        for code in re.finditer(
            r"(?=(?<![\\`])(?:\\\\)*(`+)(?!`)(.*?)(?<!`)\1(?!`))", token.content, re.S
        ):
            content = code.group(2).replace("\n", " ")
            if content.startswith(" ") and content.endswith(" ") and content.strip():
                content = content[1:-1]
            if content not in code_contents:
                continue
            code_rows.update(
                range(
                    token.content.count("\n", 0, code.start()),
                    token.content.count("\n", 0, code.end(2)) + 1,
                )
            )
        for row, line in enumerate(token.content.splitlines()):
            candidate = _ISSUE_LINK.fullmatch(line.strip())
            if candidate is None or row in code_rows:
                continue
            source_row = token.map[0] + row
            match = _ISSUE_SOURCE_LINK.fullmatch(source[source_row].rstrip("\r\n"))
            if match is not None and match.group(1) == candidate.group(1):
                offset = offsets[source_row]
                yield _IssueLink(
                    offset + match.start(1),
                    offset + match.end(1),
                    match.group(1),
                    match.group(2),
                )


def _append_trailer(body: str, trailer: str) -> str:
    result = f"{body.rstrip()}\n\n{trailer}" if body.strip() else trailer
    if not any(
        link.trailer == trailer and link.start == len(result) - len(trailer)
        for link in _issue_links(result)
    ):
        raise MemberCapabilityError(
            "Cannot append an issue link outside Markdown code or HTML. "
            "Close the open code or HTML block in the body first."
        )
    return result


def _preserve_issue_links(body: str, previous_body: str) -> str:
    """Append missing issue links while keeping the replacement body's wording."""
    mentioned = {match.number for match in _issue_links(body)}
    inherited: set[str] = set()
    for match in _issue_links(previous_body):
        number = match.number
        trailer = match.trailer
        if number not in mentioned and trailer.casefold() not in inherited:
            body = _append_trailer(body, trailer)
            inherited.add(trailer.casefold())
    return body


def _append_issue_link(body: str, issue_url: str, *, closes: bool) -> str:
    if not issue_url:
        return body
    match = re.search(r"/issues/(\d+)", issue_url)
    if not match:
        return body
    issue_number = match.group(1)
    links = [link for link in _issue_links(body) if link.number == issue_number]
    refs = [link for link in links if re.match(_ISSUE_REFS_KEYWORD, link.trailer, re.I)]
    if len(links) > len(refs):
        return body
    if refs:
        if not closes:
            return body
        for link in reversed(refs):
            body = f"{body[: link.start]}Closes #{issue_number}{body[link.end :]}"
        return body
    trailer = f"Closes #{issue_number}" if closes else f"Refs #{issue_number}"
    return _append_trailer(body, trailer)


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
