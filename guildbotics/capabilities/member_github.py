from __future__ import annotations

import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from typing import IO, Any
from urllib.parse import quote, urlparse
from zipfile import BadZipFile, ZipFile

from httpx import AsyncClient

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.actions_client import (
    GITHUB_PAGE_SIZE,
    GitHubActionsClient,
    GitHubActionsClientError,
)
from guildbotics.integrations.github.github_utils import (
    create_github_client,
    get_author_type,
    get_github_username,
)
from guildbotics.utils.person_profile import build_member_communication_style
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

REPO_WITH_OWNER_PART_COUNT = 2
GITHUB_RESOURCE_MIN_PART_COUNT = 4
GITHUB_ACTIONS_RUN_MIN_PART_COUNT = 5
DEFAULT_LOG_TAIL_BYTES = STREAM_READ_LIMIT // 32
# Artifacts are written to the isolated workspace instead of the broker output,
# so they can be larger than the shared stdout boundary while remaining bounded.
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
PATCH_HUNK_RE = re.compile(
    r"^@@ -(?P<left>\d+)(?:,(?P<left_count>\d+))? "
    r"\+(?P<right>\d+)(?:,(?P<right_count>\d+))? @@"
)
_FAILED_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "stale",
    "timed_out",
}
_PRIMARY_FAILED_CONCLUSIONS = {
    "action_required",
    "failure",
    "startup_failure",
    "timed_out",
}
_SUCCESS_CONCLUSIONS = {"neutral", "skipped", "success"}


class MemberCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubResource:
    owner: str
    repo: str
    number: int
    kind: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class GitHubPullRequestHead:
    owner: str
    repo: str
    branch: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


class MemberGitHubCapabilityService:
    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.team = team
        ticket_config = team.project.get_service_config(Service.TICKET_MANAGER)
        code_config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.base_url = str(
            code_config.get("api_base_url")
            or ticket_config.get("base_url")
            or "https://api.github.com"
        ).rstrip("/")
        self.owner = str(code_config.get("owner") or ticket_config.get("owner") or "")
        self.project_owner = str(ticket_config.get("owner") or self.owner)
        self.project_id = str(ticket_config.get("project_id") or "")
        self.project_url = str(ticket_config.get("url") or "")
        self._client: AsyncClient | None = None
        self._project_node_id: str | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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

    async def issue_inspect(self, url: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
        client = await self._get_client()
        issue_resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}"
        )
        _raise_for_status(issue_resp)
        comments_resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments"
        )
        _raise_for_status(comments_resp)
        issue = issue_resp.json()
        comments = [
            self._comment_summary(comment) for comment in _as_list(comments_resp.json())
        ]
        project_metadata = await self._issue_project_metadata(resource)
        linked_pull_request_candidates = await self._linked_pull_request_candidates(
            resource
        )
        return {
            "repo": resource.full_repo,
            "number": resource.number,
            "title": issue.get("title", ""),
            "body": issue.get("body", "") or "",
            "state": issue.get("state", ""),
            "html_url": issue.get("html_url", url),
            "assignees": [item.get("login", "") for item in issue.get("assignees", [])],
            "labels": [item.get("name", "") for item in issue.get("labels", [])],
            "project_metadata": project_metadata,
            "linked_pull_request_candidates": linked_pull_request_candidates,
            "comments": comments,
        }

    async def issue_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
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
        resp = await client.post(f"/repos/{owner}/{repo_name}/issues", json=payload)
        _raise_for_status(resp)
        issue = resp.json()
        project_item_id = None
        if add_to_project and issue.get("node_id"):
            project_item_id = await self.add_project_item(str(issue["node_id"]))
        return {
            "issue_number": issue.get("number"),
            "issue_title": issue.get("title", title),
            "repo": f"{owner}/{repo_name}",
            "issue_url": issue.get("html_url")
            or f"{self.web_base_url()}/{owner}/{repo_name}/issues/{issue.get('number')}",
            "labels": _label_names(issue),
            "project_item_id": project_item_id,
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
        client = await self._get_client()
        names: list[str] = []
        page = 1
        while True:
            resp = await client.get(
                f"/repos/{owner}/{repo}/labels",
                params={"per_page": GITHUB_PAGE_SIZE, "page": page},
            )
            _raise_for_status(resp)
            page_items = _as_list(resp.json())
            names.extend(str(item.get("name", "")) for item in page_items)
            if len(page_items) < GITHUB_PAGE_SIZE:
                break
            page += 1
        return [name for name in names if name]

    async def pr_inspect(
        self, url: str, include_comments: bool, include_diff: bool = False
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        head = self._pull_request_head(resource, pr)
        freshness = await self._pull_request_inspection_freshness(resource, pr)
        result: dict[str, Any] = {
            "repo": resource.full_repo,
            "number": resource.number,
            "title": pr.get("title", ""),
            "body": pr.get("body", "") or "",
            "state": pr.get("state", ""),
            "merged": pr.get("merged_at") is not None,
            "draft": bool(pr.get("draft", False)),
            "html_url": pr.get("html_url", url),
            "head": head.branch,
            "head_repo": head.full_repo,
            "head_owner": head.owner,
            "head_repo_name": head.repo,
            "base": (pr.get("base") or {}).get("ref", ""),
            **freshness,
        }
        if include_comments:
            result["conversation_comments"] = await self._issue_comments(resource)
            result["review_threads"] = await self._review_threads(resource)
        if include_diff:
            result["files"] = await self._pull_request_files(resource)
        return result

    async def pr_checks(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = DEFAULT_LOG_TAIL_BYTES,
    ) -> dict[str, Any]:
        """Return the checks for a PR head and optional failed Actions logs."""
        if log_tail_bytes < 1 or log_tail_bytes > STREAM_READ_LIMIT:
            raise MemberCapabilityError(
                f"log_tail_bytes must be between 1 and {STREAM_READ_LIMIT}."
            )
        resource = self.parse_url(url, expected_kind="pull")
        pr = await self._pull_request(resource)
        readiness_applies = _pull_request_readiness_applies(pr)
        freshness = (
            await self._pull_request_freshness(resource, pr)
            if readiness_applies
            else None
        )
        head_sha = self._pull_request_head_sha(resource, pr)
        actions = GitHubActionsClient(await self._get_client())
        try:
            check_runs = await actions.check_runs(
                resource.owner, resource.repo, head_sha
            )
            statuses = await actions.commit_statuses(
                resource.owner, resource.repo, head_sha
            )
            checks = _check_summaries(check_runs, statuses)
            rollup = _check_rollup(checks)
            failed_action_logs = None
            if failed_logs:
                failed_action_logs = await self._failed_action_logs(
                    actions,
                    resource,
                    head_sha,
                    log_tail_bytes,
                    check_runs,
                )
            if not readiness_applies:
                return self._pull_request_checks_not_applicable(
                    resource,
                    pr,
                    url,
                    checked_head_sha=head_sha,
                    rollup=rollup,
                    checks=checks,
                    failed_action_logs=failed_action_logs,
                )
            assert freshness is not None
            checks_expected = False
            if not checks and freshness["base_sha"] != head_sha:
                base_check_runs = await actions.check_runs(
                    resource.owner, resource.repo, freshness["base_sha"]
                )
                base_statuses = await actions.commit_statuses(
                    resource.owner, resource.repo, freshness["base_sha"]
                )
                checks_expected = bool(_check_summaries(base_check_runs, base_statuses))
            current = await self._pull_request(resource)
            if not _pull_request_readiness_applies(current):
                return self._pull_request_checks_not_applicable(
                    resource,
                    current,
                    url,
                    checked_head_sha=head_sha,
                    rollup=rollup,
                    checks=checks,
                    failed_action_logs=failed_action_logs,
                )
            current_base_sha = await self._pull_request_current_base_sha(
                resource, current
            )
            current_head_sha = self._pull_request_head_sha(resource, current)
            blockers = _completion_blockers(
                rollup=rollup,
                checks_expected=checks_expected,
                behind_by=freshness["behind_by"],
                base_sha=freshness["base_sha"],
                head_sha=head_sha,
                current_base_sha=current_base_sha,
                current_head_sha=current_head_sha,
            )
            result: dict[str, Any] = {
                "repo": resource.full_repo,
                "pr_number": resource.number,
                "pr_url": pr.get("html_url", url),
                **freshness,
                "current_base_sha": current_base_sha,
                "current_head_sha": current_head_sha,
                "rollup": rollup,
                "checks": checks,
                "checks_expected": checks_expected,
                "readiness": "blocked" if blockers else "ready",
                "completion_blockers": blockers,
            }
            if failed_action_logs is not None:
                result["failed_logs"] = failed_action_logs
            return result
        except GitHubActionsClientError as exc:
            raise MemberCapabilityError(str(exc)) from exc

    def _pull_request_checks_not_applicable(
        self,
        resource: GitHubResource,
        pr: dict[str, Any],
        url: str,
        *,
        checked_head_sha: str,
        rollup: str,
        checks: list[dict[str, Any]],
        failed_action_logs: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "repo": resource.full_repo,
            "pr_number": resource.number,
            "pr_url": pr.get("html_url", url),
            "base_sha": None,
            "head_sha": checked_head_sha,
            "behind_by": None,
            "out_of_date": None,
            "current_base_sha": None,
            "current_head_sha": self._pull_request_head_sha(resource, pr),
            "rollup": rollup,
            "checks": checks,
            "checks_expected": False,
            "readiness": "not_applicable",
            "completion_blockers": [],
        }
        if failed_action_logs is not None:
            result["failed_logs"] = failed_action_logs
        return result

    async def open_pr_checks(
        self, remote_url: str, branch: str
    ) -> list[dict[str, Any]]:
        """Return readiness for open PRs whose head is the pushed branch."""
        repository = self._repository_from_remote(remote_url)
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
                files = _extract_artifact(archive, destination)
        except GitHubActionsClientError as exc:
            raise MemberCapabilityError(str(exc)) from exc
        return {
            "repo": resource.full_repo,
            "run_id": run_id or (artifact.get("workflow_run") or {}).get("id"),
            "artifact_id": artifact_id,
            "artifact_name": name,
            "destination": str(destination.resolve()),
            "files": [str(path.resolve()) for path in files],
        }

    async def _failed_action_logs(
        self,
        actions: GitHubActionsClient,
        resource: GitHubResource,
        head_sha: str,
        log_tail_bytes: int,
        check_runs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        runs = await actions.workflow_runs(resource.owner, resource.repo, head_sha)
        failed_run_ids = _failed_actions_run_ids(check_runs)
        failed_suite_ids = {
            int((item.get("check_suite") or {}).get("id", 0))
            for item in check_runs
            if item.get("conclusion") in _FAILED_CONCLUSIONS
            and int((item.get("check_suite") or {}).get("id", 0))
        }
        failed_jobs: list[tuple[int, int, list[str], dict[str, Any]]] = []
        for run in runs:
            run_id = int(run.get("id", 0))
            if not run_id:
                continue
            if failed_run_ids and run_id not in failed_run_ids:
                continue
            if (
                not failed_run_ids
                and failed_suite_ids
                and int(run.get("check_suite_id", 0)) not in failed_suite_ids
            ):
                continue
            if (
                not failed_run_ids
                and not failed_suite_ids
                and run.get("conclusion") not in _FAILED_CONCLUSIONS
            ):
                continue
            jobs = await actions.jobs(resource.owner, resource.repo, run_id)
            run_failed_jobs = [
                job
                for job in jobs
                if job.get("conclusion") in _FAILED_CONCLUSIONS
                and int(job.get("id", 0))
            ]
            if not run_failed_jobs:
                continue
            artifacts = await actions.artifacts(
                resource.owner, resource.repo, run_id=run_id
            )
            artifact_names = sorted(
                {
                    str(artifact.get("name", ""))
                    for artifact in artifacts
                    if artifact.get("name") and not artifact.get("expired", False)
                }
            )
            run_attempt = int(run.get("run_attempt", 1))
            failed_jobs.extend(
                (run_id, run_attempt, artifact_names, job) for job in run_failed_jobs
            )
        if not failed_jobs:
            return []
        primary_failed_jobs = [
            item
            for item in failed_jobs
            if item[3].get("conclusion") in _PRIMARY_FAILED_CONCLUSIONS
        ]
        # Cancelled and stale matrix jobs are usually fallout from the real
        # failure. Return them only when there is no primary failure to inspect.
        selected_jobs = primary_failed_jobs or failed_jobs
        # Leave room for JSON escaping and per-job metadata inside the broker's
        # output boundary. With a small failed set this remains the requested
        # per-job tail; a large matrix is divided fairly instead of producing
        # truncated, invalid JSON at the broker boundary.
        effective_tail_bytes = min(
            log_tail_bytes,
            max(1, STREAM_READ_LIMIT // (8 * len(selected_jobs))),
        )
        logs: list[dict[str, Any]] = []
        for run_id, run_attempt, artifact_names, job in selected_jobs:
            job_id = int(job["id"])
            tail, truncated = await actions.job_log_tail(
                resource.owner,
                resource.repo,
                job_id,
                effective_tail_bytes,
            )
            logs.append(
                {
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                    "job_id": job_id,
                    "name": job.get("name", ""),
                    "conclusion": job.get("conclusion", ""),
                    "html_url": job.get("html_url", ""),
                    "artifact_names": artifact_names,
                    "log": tail.decode("utf-8", errors="replace"),
                    "log_bytes": len(tail),
                    "tail_limit_bytes": effective_tail_bytes,
                    "truncated": truncated,
                }
            )
        return logs

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
            }

        body = _append_issue_link(body, issue_url, closes=closes_issue)
        payload: dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base_branch,
            "body": body,
            "draft": draft == "true",
        }
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
        }

    async def pr_update(
        self, url: str, body: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if not payload:
            raise MemberCapabilityError("pr update needs a body or a title.")
        client = await self._get_client()
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
        }

    async def pr_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        return _comment_result(comment)

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
        }

    async def pr_reply(
        self, url: str, reply_target_id: int, body: str
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        threads = await self._review_threads(resource)
        allowed = {
            int(thread["reply_target_id"])
            for thread in threads
            if thread.get("replyable") and thread.get("reply_target_id") is not None
        }
        if reply_target_id not in allowed:
            raise MemberCapabilityError(
                f"Review comment '{reply_target_id}' is not replyable for this PR."
            )
        client = await self._get_client()
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

    def _repository_from_remote(self, remote_url: str) -> tuple[str, str] | None:
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
        return login.lower() == get_github_username(self.person).lower()

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

    async def get_pr_head_branch(self, url: str) -> str:
        return (await self.get_pr_head(url)).branch

    async def get_pr_head(self, url: str) -> GitHubPullRequestHead:
        resource = self.parse_url(url, expected_kind="pull")
        return self._pull_request_head(resource, await self._pull_request(resource))

    async def _pull_request(self, resource: GitHubResource) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}"
        )
        _raise_for_status(resp)
        payload = resp.json()
        if not isinstance(payload, dict):
            raise MemberCapabilityError(
                "Unexpected GitHub pull request response for "
                f"{resource.full_repo}#{resource.number}."
            )
        return payload

    async def _pull_request_freshness(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> dict[str, Any]:
        base_sha = await self._pull_request_current_base_sha(resource, pr)
        head_sha = self._pull_request_head_sha(resource, pr)
        client = await self._get_client()
        response = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/compare/"
            f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        )
        _raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("behind_by"), int
        ):
            raise MemberCapabilityError(
                "Unexpected GitHub compare response for "
                f"{resource.full_repo}#{resource.number}."
            )
        behind_by = payload["behind_by"]
        if behind_by < 0:
            raise MemberCapabilityError(
                "Unexpected GitHub compare response for "
                f"{resource.full_repo}#{resource.number}."
            )
        return {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "behind_by": behind_by,
            "out_of_date": behind_by > 0,
        }

    async def _pull_request_inspection_freshness(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> dict[str, Any]:
        head_sha = self._pull_request_head_sha(resource, pr)
        unavailable: dict[str, Any] = {
            "base_sha": None,
            "head_sha": head_sha,
            "behind_by": None,
            "out_of_date": None,
        }
        if not _pull_request_readiness_applies(pr):
            return unavailable
        try:
            return await self._pull_request_freshness(resource, pr)
        except MemberCapabilityError as exc:
            unavailable["freshness_error"] = str(exc)
            return unavailable

    async def _pull_request_current_base_sha(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> str:
        branch = self._pull_request_base_ref(resource, pr)
        client = await self._get_client()
        response = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/branches/{quote(branch, safe='')}"
        )
        _raise_for_status(response)
        payload = response.json()
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        if not sha:
            raise MemberCapabilityError(
                "Unexpected GitHub branch response for "
                f"{resource.full_repo}#{resource.number}."
            )
        return sha

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

    def parse_url(self, url: str, expected_kind: str | None = None) -> GitHubResource:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < GITHUB_RESOURCE_MIN_PART_COUNT:
            raise MemberCapabilityError(f"Unsupported GitHub URL: {url}")
        owner, repo, kind, number_text = parts[0], parts[1], parts[2], parts[3]
        if kind == "pull":
            normalized_kind = "pull"
        elif kind == "issues":
            normalized_kind = "issue"
        else:
            raise MemberCapabilityError(f"Unsupported GitHub URL kind '{kind}'.")
        if expected_kind and normalized_kind != expected_kind:
            raise MemberCapabilityError(
                f"Expected {expected_kind} URL, got {normalized_kind} URL."
            )
        return GitHubResource(
            owner=owner,
            repo=repo,
            number=int(number_text),
            kind=normalized_kind,
        )

    def web_base_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base in ("https://api.github.com", "http://api.github.com"):
            return "https://github.com"
        for suffix in ("/api/v3", "/api"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    async def add_project_item(self, issue_node_id: str) -> str | None:
        if not self.project_id:
            return None
        project_node_id = await self._project_node()
        mutation = """
        mutation($proj: ID!, $content: ID!) {
          addProjectV2ItemById(input:{ projectId: $proj, contentId: $content }) {
            item { id }
          }
        }
        """
        data = await self._graphql(
            mutation, {"proj": project_node_id, "content": issue_node_id}
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

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = await create_github_client(self.person, self.base_url)
        return self._client

    async def _issue_comments(self, resource: GitHubResource) -> list[dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments"
        )
        _raise_for_status(resp)
        return [self._comment_summary(comment) for comment in _as_list(resp.json())]

    async def _pull_request_files(
        self, resource: GitHubResource
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        endpoint = (
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/files"
        )
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await client.get(
                endpoint, params={"per_page": GITHUB_PAGE_SIZE, "page": page}
            )
            _raise_for_status(resp)
            page_items = _as_list(resp.json())
            files.extend(self._pull_request_file_summary(item) for item in page_items)
            if len(page_items) < GITHUB_PAGE_SIZE:
                break
            page += 1
        return files

    async def _review_threads(self, resource: GitHubResource) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 50, after: $after) {
                nodes {
                  isResolved
                  isOutdated
                  comments(first: 50) {
                    nodes {
                      databaseId
                      body
                      createdAt
                      url
                      author { login }
                      replyTo { databaseId }
                    }
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
        """
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._graphql(
                query,
                {
                    "owner": resource.owner,
                    "repo": resource.repo,
                    "number": resource.number,
                    "after": after,
                },
            )
            pull_request = (data.get("repository") or {}).get("pullRequest")
            if not pull_request:
                raise MemberCapabilityError(
                    f"Pull request review threads unavailable for {resource.full_repo}#{resource.number}."
                )
            review_threads = pull_request["reviewThreads"]
            nodes.extend(review_threads.get("nodes") or [])
            page_info = review_threads["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]

        threads: list[dict[str, Any]] = []
        for thread in nodes:
            comments = [
                comment
                for comment in ((thread.get("comments") or {}).get("nodes") or [])
                if comment.get("databaseId") is not None
            ]
            if not comments:
                continue
            comments.sort(key=lambda item: str(item.get("createdAt", "")))
            root = next(
                (comment for comment in comments if not comment.get("replyTo")),
                comments[0],
            )
            latest = comments[-1]
            root_id = int(root["databaseId"])
            latest_id = int(latest["databaseId"])
            resolved = bool(thread.get("isResolved"))
            outdated = bool(thread.get("isOutdated"))
            replyable = True
            threads.append(
                {
                    "root_comment_id": root_id,
                    "latest_comment_id": latest_id,
                    "resolved": resolved,
                    "outdated": outdated,
                    "replyable": replyable,
                    "reply_target_id": root_id if replyable else None,
                    "comments": [
                        self._graphql_review_comment_summary(comment)
                        for comment in comments
                    ],
                }
            )
        return threads

    async def _linked_pull_request_candidates(
        self, resource: GitHubResource
    ) -> list[dict[str, Any]]:
        urls = await self._linked_pull_request_urls(resource)
        candidates = []
        for url in urls:
            try:
                pr_resource = self.parse_url(url, expected_kind="pull")
            except Exception:
                continue
            pr = await self.pr_inspect(url, include_comments=False)
            candidates.append(
                {
                    "number": pr_resource.number,
                    "url": pr.get("html_url", url),
                    "title": pr.get("title", ""),
                    "state": pr.get("state", ""),
                    "merged": pr.get("merged", False),
                }
            )
        return candidates

    async def _linked_pull_request_urls(self, resource: GitHubResource) -> list[str]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/timeline",
            headers={"Accept": "application/vnd.github+json"},
        )
        try:
            resp.raise_for_status()
        except Exception:
            return []

        urls: list[str] = []
        for event in _as_list(resp.json()):
            source = event.get("source", {})
            issue = source.get("issue", {}) if isinstance(source, dict) else {}
            if "pull_request" in issue and issue.get("html_url"):
                urls.append(str(issue["html_url"]))
        return list(dict.fromkeys(urls))

    async def _issue_project_metadata(self, resource: GitHubResource) -> dict[str, Any]:
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              projectItems(first: 20) {
                nodes {
                  id
                  project { title number url }
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldTextValue {
                        text
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldDateValue {
                        date
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldNumberValue {
                        number
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = await self._graphql(
                query,
                {
                    "owner": resource.owner,
                    "repo": resource.repo,
                    "number": resource.number,
                },
            )
        except Exception:
            return {}
        issue = ((data.get("repository") or {}).get("issue")) or {}
        items = ((issue.get("projectItems") or {}).get("nodes")) or []
        metadata = [_project_item_summary(item) for item in items if item]
        return {"items": metadata} if metadata else {}

    async def _post_comment(self, endpoint: str, body: str) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.post(endpoint, json={"body": body.rstrip()})
        _raise_for_status(resp)
        return resp.json()

    def _comment_summary(self, comment: dict[str, Any]) -> dict[str, Any]:
        body = str(comment.get("body") or "")
        user = comment.get("user") or {}
        login = str(user.get("login") or "")
        return {
            "id": comment.get("id"),
            "body": body,
            "author": login,
            "author_type": get_author_type(self.person, login) if login else "",
            "created_at": comment.get("created_at"),
            "html_url": comment.get("html_url"),
        }

    def _pull_request_file_summary(self, file: dict[str, Any]) -> dict[str, Any]:
        path = str(file.get("filename") or "")
        patch = str(file.get("patch") or "")
        return {
            "path": path,
            "status": file.get("status", ""),
            "additions": file.get("additions", 0),
            "deletions": file.get("deletions", 0),
            "changes": file.get("changes", 0),
            "commentable_lines": _commentable_lines_from_patch(path, patch),
        }

    def _graphql_review_comment_summary(
        self, comment: dict[str, Any]
    ) -> dict[str, Any]:
        body = str(comment.get("body") or "")
        author = comment.get("author") or {}
        login = str(author.get("login") or "")
        return {
            "id": comment.get("databaseId"),
            "body": body,
            "author": login,
            "author_type": get_author_type(self.person, login) if login else "",
            "created_at": comment.get("createdAt"),
            "html_url": comment.get("url"),
        }

    def _pull_request_head(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> GitHubPullRequestHead:
        raw_head = pr.get("head")
        head = raw_head if isinstance(raw_head, dict) else {}
        branch = str(head.get("ref") or "")
        raw_repo = head.get("repo")
        repo = raw_repo if isinstance(raw_repo, dict) else {}
        full_name = str(repo.get("full_name") or "")
        raw_owner = repo.get("owner")
        repo_owner = raw_owner if isinstance(raw_owner, dict) else {}
        owner = str(repo_owner.get("login") or "")
        repo_name = str(repo.get("name") or "")
        if full_name and "/" in full_name:
            owner, repo_name = full_name.split("/", 1)
        if not branch:
            raise MemberCapabilityError(
                f"Pull request head branch not found for {resource.full_repo}#{resource.number}."
            )
        if not owner or not repo_name:
            label = str(head.get("label") or "")
            if label.startswith(f"{resource.owner}:"):
                owner, repo_name = resource.owner, resource.repo
            else:
                raise MemberCapabilityError(
                    f"Pull request head repository not found for {resource.full_repo}#{resource.number}."
                )
        return GitHubPullRequestHead(owner=owner, repo=repo_name, branch=branch)

    def _pull_request_head_sha(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> str:
        raw_head = pr.get("head")
        head = raw_head if isinstance(raw_head, dict) else {}
        sha = str(head.get("sha") or "")
        if not sha:
            raise MemberCapabilityError(
                f"Pull request head commit not found for {resource.full_repo}#{resource.number}."
            )
        return sha

    def _pull_request_base_ref(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> str:
        raw_base = pr.get("base")
        base = raw_base if isinstance(raw_base, dict) else {}
        branch = str(base.get("ref") or "")
        if not branch:
            raise MemberCapabilityError(
                f"Pull request base branch not found for {resource.full_repo}#{resource.number}."
            )
        return branch


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


def _raise_for_status(resp: Any) -> None:
    try:
        resp.raise_for_status()
    except Exception as exc:
        status_code = getattr(resp, "status_code", "")
        raise MemberCapabilityError(
            f"GitHub API request failed with status {status_code}."
        ) from exc


def _comment_result(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "comment_id": comment.get("id"),
        "html_url": comment.get("html_url"),
        "author": user.get("login", ""),
        "created_at": comment.get("created_at"),
    }


def _check_summaries(
    check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checks = [
        {
            "name": item.get("name", ""),
            "status": item.get("status", ""),
            "conclusion": item.get("conclusion"),
            "details_url": item.get("details_url", ""),
            "source": "check_run",
        }
        for item in check_runs
    ]
    checks.extend(
        {
            "name": item.get("context", ""),
            "status": "completed" if item.get("state") != "pending" else "pending",
            "conclusion": item.get("state"),
            "details_url": item.get("target_url", ""),
            "source": "commit_status",
        }
        for item in statuses
    )
    return checks


def _check_rollup(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "no_checks"
    if any(
        item.get("conclusion") in _FAILED_CONCLUSIONS | {"error"} for item in checks
    ):
        return "failure"
    if any(item.get("status") != "completed" for item in checks):
        return "pending"
    if all(item.get("conclusion") in _SUCCESS_CONCLUSIONS for item in checks):
        return "success"
    return "pending"


def _pull_request_readiness_applies(pr: dict[str, Any]) -> bool:
    return pr.get("state") == "open"


def _completion_blockers(
    *,
    rollup: str,
    checks_expected: bool,
    behind_by: int,
    base_sha: str,
    head_sha: str,
    current_base_sha: str,
    current_head_sha: str,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if behind_by > 0:
        blockers.append(
            {
                "code": "base_out_of_date",
                "message": f"The PR head is {behind_by} commit(s) behind its base.",
                "next_action": "Merge or rebase the base branch, push, and check again.",
            }
        )
    if current_head_sha != head_sha:
        blockers.append(
            {
                "code": "head_changed",
                "message": "The PR head changed while readiness was being checked.",
                "next_action": "Check the new head SHA and its CI results again.",
            }
        )
    if current_base_sha != base_sha:
        blockers.append(
            {
                "code": "base_changed",
                "message": "The PR base changed while readiness was being checked.",
                "next_action": "Check freshness against the new base SHA again.",
            }
        )
    if rollup == "failure":
        blockers.append(
            {
                "code": "checks_failed",
                "message": "CI checks are failing.",
                "next_action": "Inspect failed logs, fix the failures, and check again.",
            }
        )
    elif rollup == "pending":
        blockers.append(
            {
                "code": "checks_pending",
                "message": "CI checks are still pending.",
                "next_action": "Wait for every check to finish, then check again.",
            }
        )
    elif rollup == "no_checks" and checks_expected:
        blockers.append(
            {
                "code": "checks_pending",
                "message": "CI checks have not been registered for the PR head yet.",
                "next_action": "Wait for CI to start, then check again.",
            }
        )
    return blockers


def _failed_actions_run_ids(check_runs: list[dict[str, Any]]) -> set[int]:
    run_ids: set[int] = set()
    for check in check_runs:
        if check.get("conclusion") not in _FAILED_CONCLUSIONS:
            continue
        parsed = urlparse(str(check.get("details_url", "")))
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < GITHUB_ACTIONS_RUN_MIN_PART_COUNT or parts[2:4] != [
            "actions",
            "runs",
        ]:
            continue
        try:
            run_ids.add(int(parts[4]))
        except ValueError:
            continue
    return run_ids


def _extract_artifact(archive: IO[bytes], destination: Path) -> list[Path]:
    destination = destination.resolve()
    try:
        with ZipFile(archive) as bundle:
            members = bundle.infolist()
            total_size = sum(member.file_size for member in members)
            if total_size > MAX_ARTIFACT_BYTES:
                raise MemberCapabilityError(
                    "Expanded artifact is "
                    f"{total_size} bytes, above the {MAX_ARTIFACT_BYTES} byte limit."
                )
            targets: list[tuple[Any, Path]] = []
            seen: set[Path] = set()
            for member in members:
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                if (
                    relative == PurePosixPath(".")
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    raise MemberCapabilityError(
                        f"Artifact contains an unsafe path: {member.filename}"
                    )
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise MemberCapabilityError(
                        f"Artifact contains an unsupported symlink: {member.filename}"
                    )
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(destination):
                    raise MemberCapabilityError(
                        f"Artifact contains an unsafe path: {member.filename}"
                    )
                if target in seen:
                    raise MemberCapabilityError(
                        f"Artifact contains a duplicate path: {member.filename}"
                    )
                seen.add(target)
                targets.append((member, target))
            collisions = [
                target
                for member, target in targets
                if not member.is_dir() and target.exists()
            ]
            if collisions:
                raise MemberCapabilityError(
                    f"Artifact destination already exists: {collisions[0]}. "
                    "Choose a different --dest or remove the existing file, then retry."
                )
            destination.mkdir(parents=True, exist_ok=True)
            files: list[Path] = []
            for member, target in targets:
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    copyfileobj(source, output)
                files.append(target)
            return files
    except BadZipFile as exc:
        raise MemberCapabilityError(
            "GitHub artifact is not a valid ZIP archive."
        ) from exc


def _commentable_lines_from_patch(path: str, patch: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    left_line: int | None = None
    right_line: int | None = None
    for raw_line in patch.splitlines():
        hunk = PATCH_HUNK_RE.match(raw_line)
        if hunk:
            left_line = int(hunk.group("left"))
            right_line = int(hunk.group("right"))
            continue
        if left_line is None or right_line is None or raw_line.startswith("\\"):
            continue
        marker = raw_line[:1]
        content = raw_line[1:] if marker in {" ", "+", "-"} else raw_line
        if marker == " ":
            lines.append(
                {
                    "path": path,
                    "line": right_line,
                    "side": "RIGHT",
                    "left_line": left_line,
                    "right_line": right_line,
                    "content": content,
                }
            )
            left_line += 1
            right_line += 1
        elif marker == "+":
            lines.append(
                {
                    "path": path,
                    "line": right_line,
                    "side": "RIGHT",
                    "right_line": right_line,
                    "content": content,
                }
            )
            right_line += 1
        elif marker == "-":
            lines.append(
                {
                    "path": path,
                    "line": left_line,
                    "side": "LEFT",
                    "left_line": left_line,
                    "content": content,
                }
            )
            left_line += 1
    return lines


def _project_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    project = item.get("project") or {}
    field_values = []
    for value in ((item.get("fieldValues") or {}).get("nodes")) or []:
        field = value.get("field") or {}
        field_name = field.get("name")
        if not field_name:
            continue
        field_values.append(
            {
                "field": field_name,
                "value": value.get("name")
                or value.get("text")
                or value.get("date")
                or value.get("number"),
            }
        )
    return {
        "item_id": item.get("id"),
        "project_title": project.get("title"),
        "project_number": project.get("number"),
        "project_url": project.get("url"),
        "fields": field_values,
    }


_ISSUE_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)"
_ISSUE_REFS_KEYWORD = r"refs?"


def _append_issue_link(body: str, issue_url: str, *, closes: bool) -> str:
    if not issue_url:
        return body
    match = re.search(r"/issues/(\d+)", issue_url)
    if not match:
        return body
    issue_number = match.group(1)
    closing_ref = rf"\b{_ISSUE_CLOSING_KEYWORD}\s+#{issue_number}\b"
    refs_ref = rf"\b{_ISSUE_REFS_KEYWORD}\s+#{issue_number}\b"
    if re.search(closing_ref, body, re.I):
        return body
    if re.search(refs_ref, body, re.I):
        if not closes:
            return body
        return re.sub(refs_ref, f"Closes #{issue_number}", body, flags=re.I)
    trailer = f"Closes #{issue_number}" if closes else f"Refs #{issue_number}"
    return f"{body.rstrip()}\n\n{trailer}" if body.strip() else trailer


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
