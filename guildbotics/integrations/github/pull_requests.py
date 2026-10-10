"""GitHub's REST client for a member, its targets, and pull-request
completion readiness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from httpx import AsyncClient

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.github.actions_client import (
    GitHubActionsClient,
    GitHubActionsClientError,
)
from guildbotics.integrations.github.github_utils import create_github_client
from guildbotics.integrations.repository_scope import configured_owner
from guildbotics.runtime.code_hosting_resources import (
    MAX_PAGE_BYTES,
    Readiness,
    ReadinessQuery,
)
from guildbotics.runtime.code_hosting_service import PullRequestHead
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.utils.i18n_tool import t

GITHUB_RESOURCE_MIN_PART_COUNT = 4


GITHUB_ACTIONS_RUN_MIN_PART_COUNT = 5


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


@dataclass(frozen=True)
class GitHubResource:
    owner: str
    repo: str
    number: int
    kind: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


def _raise_for_status(resp: Any) -> None:
    try:
        resp.raise_for_status()
    except Exception as exc:
        status_code = getattr(resp, "status_code", "")
        raise MemberCapabilityError(
            f"GitHub API request failed with status {status_code}."
        ) from exc


def _work_target(
    resource: GitHubResource, item: dict[str, Any], url: str = ""
) -> dict[str, Any]:
    """The PR / issue a command worked on, in one shape for every command.

    The member CLI records it as the work target of the trace it runs inside,
    so each command reports it the same way whatever else it returns.
    """
    return {
        "kind": "pull_request" if resource.kind == "pull" else "issue",
        "repo": resource.full_repo,
        "number": item.get("number", resource.number),
        "title": str(item.get("title") or ""),
        "html_url": str(item.get("html_url") or url),
    }


def _check_summaries(
    check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checks = [
        {
            "name": str(item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "conclusion": item.get("conclusion"),
            "details_url": str(item.get("details_url") or ""),
            "source": "check_run",
        }
        for item in check_runs
    ]
    checks.extend(
        {
            "name": str(item.get("context") or ""),
            "status": "completed" if item.get("state") != "pending" else "pending",
            "conclusion": item.get("state"),
            "details_url": str(item.get("target_url") or ""),
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


def _fit_log_tails(result: dict[str, Any]) -> None:
    """Fit log tails after reserving the actual JSON page and job metadata."""
    logs = result["failed_logs"]
    if not logs:
        return
    tails = [item["log"] for item in logs]
    for item in logs:
        item["log_bytes"] = len(item["log"].encode())
        item["log"] = ""
    # The page the result is read in (RepositoryReadPage), as it serializes.
    page = {"items": [result], "continuation": None, "target": result["target"]}
    remaining = MAX_PAGE_BYTES - len(json.dumps(page).encode())
    if remaining < 0:
        raise MemberCapabilityError(t("integrations.repository.too_large"))
    budget = remaining // len(logs)
    for item, tail in zip(logs, tails, strict=True):
        low, high = 0, len(tail)
        while low < high:
            length = (low + high + 1) // 2
            if len(json.dumps(tail[-length:]).encode()) - 2 <= budget:
                low = length
            else:
                high = length - 1
        item["log"] = tail[-low:] if low else ""
        item["log_bytes"] = len(item["log"].encode())
        if low < len(tail):
            item["tail_limit_bytes"] = min(item["tail_limit_bytes"], item["log_bytes"])
            item["truncated"] = True


class GitHubPullRequests:
    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.team = team
        config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.base_url = str(
            config.get("api_base_url") or "https://api.github.com"
        ).rstrip("/")
        self.owner = configured_owner(team.project)
        self._client: AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        """Return the checks for a PR head and optional failed Actions logs."""
        return Readiness.model_validate(
            await self._readiness(url, failed_logs, log_tail_bytes)
        )

    async def _readiness(
        self, url: str, failed_logs: bool, log_tail_bytes: int
    ) -> dict[str, Any]:
        ReadinessQuery(failed_logs=failed_logs, log_tail_bytes=log_tail_bytes)
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
                "target": _work_target(resource, pr, url),
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
                _fit_log_tails(result)
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
            "target": _work_target(resource, pr, url),
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
            _fit_log_tails(result)
        return result

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
        # Bound the initial downloads; exact serialized metadata and escaping
        # are accounted for after the final head/base check by _fit_log_tails.
        effective_tail_bytes = min(
            log_tail_bytes,
            max(1, MAX_PAGE_BYTES // len(selected_jobs)),
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
                    "name": str(job.get("name") or ""),
                    "conclusion": str(job.get("conclusion") or ""),
                    "html_url": str(job.get("html_url") or ""),
                    "artifact_names": artifact_names,
                    "log": tail.decode("utf-8", errors="replace"),
                    "log_bytes": len(tail),
                    "tail_limit_bytes": effective_tail_bytes,
                    "truncated": truncated,
                }
            )
        return logs

    async def _pull_request(self, resource: GitHubResource) -> dict[str, Any]:
        return await self._item(resource, "pulls", "pull request")

    async def _issue(self, resource: GitHubResource) -> dict[str, Any]:
        return await self._item(resource, "issues", "issue")

    async def _item(
        self, resource: GitHubResource, collection: str, noun: str
    ) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/{collection}/{resource.number}"
        )
        _raise_for_status(resp)
        payload = resp.json()
        if not isinstance(payload, dict):
            raise MemberCapabilityError(
                f"Unexpected GitHub {noun} response for "
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

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = await create_github_client(
                self.person,
                self.base_url,
                self.owner,
            )
        return self._client

    def _pull_request_head(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> PullRequestHead:
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
        return PullRequestHead(owner=owner, repo=repo_name, branch=branch)

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
