"""A pull request on GitHub: its head, its base, and whether its head may be
reported complete."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlparse

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.github.actions_client import GitHubActionsClient
from guildbotics.integrations.github.api import GitHubApi, GitHubItem
from guildbotics.runtime.code_hosting_service import PullRequestHead
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import (
    MAX_PAGE_BYTES,
    Readiness,
    ReadinessQuery,
)
from guildbotics.utils.i18n_tool import t

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


def _check_summaries(
    check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checks = [
        {
            "name": str(item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "conclusion": item.get("conclusion"),
            "details_url": str(item.get("details_url") or ""),
        }
        for item in check_runs
    ]
    checks.extend(
        {
            "name": str(item.get("context") or ""),
            "status": "completed" if item.get("state") != "pending" else "pending",
            "conclusion": item.get("state"),
            "details_url": str(item.get("target_url") or ""),
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
        raise ServiceError(t("integrations.resources.too_large"))
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


class GitHubPullRequests(GitHubApi):
    def __init__(self, person: Person, team: Team) -> None:
        super().__init__(person, team, Service.CODE_HOSTING_SERVICE)

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        """Return the checks for a PR head and optional failed Actions logs."""
        ReadinessQuery(failed_logs=failed_logs, log_tail_bytes=log_tail_bytes)
        item = self.item(url, "pull_request")
        pr = await self.fetch(item)
        readiness_applies = pr.get("state") == "open"
        freshness = await self._freshness(item, pr) if readiness_applies else None
        head_sha = head_sha_of(item, pr)
        actions = GitHubActionsClient(await self.client())
        check_runs = await actions.check_runs(item.repository, head_sha)
        statuses = await actions.commit_statuses(item.repository, head_sha)
        checks = _check_summaries(check_runs, statuses)
        rollup = _check_rollup(checks)
        logs = (
            await self._failed_logs(actions, item, head_sha, log_tail_bytes, check_runs)
            if failed_logs
            else None
        )
        current = pr
        result: dict[str, Any] = {
            "base_sha": None,
            "head_sha": head_sha,
            "behind_by": None,
            "out_of_date": None,
            "current_base_sha": None,
            "rollup": rollup,
            "checks": checks,
            "checks_expected": False,
            "readiness": "not_applicable",
            "completion_blockers": [],
        }
        if freshness is not None:
            if not checks and freshness["base_sha"] != head_sha:
                base_check_runs = await actions.check_runs(
                    item.repository, freshness["base_sha"]
                )
                base_statuses = await actions.commit_statuses(
                    item.repository, freshness["base_sha"]
                )
                result["checks_expected"] = bool(
                    _check_summaries(base_check_runs, base_statuses)
                )
            current = await self.fetch(item)
            if current.get("state") == "open":
                current_base_sha = await self._base_sha(item, current)
                blockers = _completion_blockers(
                    rollup=rollup,
                    checks_expected=result["checks_expected"],
                    behind_by=freshness["behind_by"],
                    base_sha=freshness["base_sha"],
                    head_sha=head_sha,
                    current_base_sha=current_base_sha,
                    current_head_sha=head_sha_of(item, current),
                )
                result.update(
                    freshness,
                    current_base_sha=current_base_sha,
                    readiness="blocked" if blockers else "ready",
                    completion_blockers=blockers,
                )
            else:
                result["checks_expected"] = False
        result["target"] = self.target(item, current).model_dump()
        result["current_head_sha"] = head_sha_of(item, current)
        if logs is not None:
            result["failed_logs"] = logs
            _fit_log_tails(result)
        return Readiness.model_validate(result)

    async def _failed_logs(
        self,
        actions: GitHubActionsClient,
        item: GitHubItem,
        head_sha: str,
        log_tail_bytes: int,
        check_runs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        runs = await actions.workflow_runs(item.repository, head_sha)
        failed_run_ids = _failed_actions_run_ids(check_runs)
        failed_suite_ids = {
            int((check.get("check_suite") or {}).get("id", 0))
            for check in check_runs
            if check.get("conclusion") in _FAILED_CONCLUSIONS
            and int((check.get("check_suite") or {}).get("id", 0))
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
            jobs = await actions.jobs(item.repository, run_id)
            run_failed_jobs = [
                job
                for job in jobs
                if job.get("conclusion") in _FAILED_CONCLUSIONS
                and int(job.get("id", 0))
            ]
            if not run_failed_jobs:
                continue
            artifacts = await actions.artifacts(item.repository, run_id=run_id)
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
            job
            for job in failed_jobs
            if job[3].get("conclusion") in _PRIMARY_FAILED_CONCLUSIONS
        ]
        # Cancelled and stale matrix jobs are usually fallout from the real
        # failure. Return them only when there is no primary failure to inspect.
        selected_jobs = primary_failed_jobs or failed_jobs
        # Bound the initial downloads; exact serialized metadata and escaping
        # are accounted for after the final head/base check by _fit_log_tails.
        effective_tail_bytes = min(
            log_tail_bytes, max(1, MAX_PAGE_BYTES // len(selected_jobs))
        )
        logs: list[dict[str, Any]] = []
        for run_id, run_attempt, artifact_names, job in selected_jobs:
            job_id = int(job["id"])
            tail, truncated = await actions.job_log_tail(
                item.repository, job_id, effective_tail_bytes
            )
            logs.append(
                {
                    # GitHub retries a whole run: its attempt names the job's.
                    "job_id": f"{run_id}/{run_attempt}/{job_id}",
                    "name": str(job.get("name") or ""),
                    "conclusion": str(job.get("conclusion") or ""),
                    "url": str(job.get("html_url") or ""),
                    "artifact_names": artifact_names,
                    "log": tail.decode("utf-8", errors="replace"),
                    "log_bytes": len(tail),
                    "tail_limit_bytes": effective_tail_bytes,
                    "truncated": truncated,
                }
            )
        return logs

    async def _freshness(self, item: GitHubItem, pr: dict[str, Any]) -> dict[str, Any]:
        base_sha = await self._base_sha(item, pr)
        head_sha = head_sha_of(item, pr)
        payload = await self.get(
            f"/repos/{item.repository}/compare/"
            f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        )
        behind_by = payload.get("behind_by") if isinstance(payload, dict) else None
        if not isinstance(behind_by, int) or behind_by < 0:
            raise ServiceError(t("integrations.github.response.response"))
        return {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "behind_by": behind_by,
            "out_of_date": behind_by > 0,
        }

    async def _base_sha(self, item: GitHubItem, pr: dict[str, Any]) -> str:
        branch = str((pr.get("base") or {}).get("ref") or "")
        if not branch:
            raise ServiceError(f"{self.url(item)} has no base branch.")
        payload = await self.get(
            f"/repos/{item.repository}/branches/{quote(branch, safe='')}"
        )
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        if not sha:
            raise ServiceError(t("integrations.github.response.response"))
        return sha


def head_of(item: GitHubItem, pr: dict[str, Any]) -> PullRequestHead:
    """The repository and branch the pull request's head is on.

    Raises:
        ServiceError: If GitHub does not say, as for a deleted fork.
    """
    head = pr.get("head") or {}
    branch = str(head.get("ref") or "")
    repository = str((head.get("repo") or {}).get("full_name") or "")
    if not repository and str(head.get("label") or "").startswith(
        f"{item.repository.partition('/')[0]}:"
    ):
        repository = item.repository
    if not branch or not repository:
        raise ServiceError(
            f"The head of {item.repository}#{item.number} is not on a repository."
        )
    return PullRequestHead(repository=repository, branch=branch)


def head_sha_of(item: GitHubItem, pr: dict[str, Any]) -> str:
    sha = str((pr.get("head") or {}).get("sha") or "")
    if not sha:
        raise ServiceError(f"{item.repository}#{item.number} has no head commit.")
    return sha
