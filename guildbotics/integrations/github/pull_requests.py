"""A pull request on GitHub: its head, its base, and whether its head may be
reported complete."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.github.actions_client import GitHubActionsClient
from guildbotics.integrations.github.api import GitHubApi, GitHubItem
from guildbotics.integrations.readiness import (
    FAILED_CONCLUSIONS,
    Freshness,
    effective_tail_bytes,
    inspected,
    readiness_of,
)
from guildbotics.runtime.code_hosting_service import PullRequestHead
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import Readiness, ReadinessQuery
from guildbotics.utils.i18n_tool import t

GITHUB_ACTIONS_RUN_MIN_PART_COUNT = 5


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


def _failed_actions_run_ids(check_runs: list[dict[str, Any]]) -> set[int]:
    run_ids: set[int] = set()
    for check in check_runs:
        if check.get("conclusion") not in FAILED_CONCLUSIONS:
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
        base_sha = await self._base_sha(item, pr) if pr.get("state") == "open" else ""
        head_sha = head_sha_of(item, pr)
        behind_by = await self._behind_by(item, base_sha, head_sha) if base_sha else 0
        actions = GitHubActionsClient(await self.client())
        check_runs = await actions.check_runs(item.repository, head_sha)
        statuses = await actions.commit_statuses(item.repository, head_sha)
        checks = _check_summaries(check_runs, statuses)
        logs = (
            await self._failed_logs(actions, item, head_sha, log_tail_bytes, check_runs)
            if failed_logs
            else None
        )
        current, freshness, checks_expected = pr, None, False
        if base_sha:
            if not checks and base_sha != head_sha:
                checks_expected = bool(
                    _check_summaries(
                        await actions.check_runs(item.repository, base_sha),
                        await actions.commit_statuses(item.repository, base_sha),
                    )
                )
            current = await self.fetch(item)
            if current.get("state") == "open":
                # Only explicitly mergeable states establish that lag is allowed.
                # Blocked or undetermined states may hide strict branch protection.
                # https://docs.github.com/en/graphql/reference/pulls#mergestatestatus
                freshness = Freshness(
                    base_sha,
                    behind_by,
                    await self._base_sha(item, current),
                    requires_up_to_date=current.get("mergeable_state")
                    not in ("clean", "unstable", "has_hooks"),
                )
        return readiness_of(
            target=self.target(item, current),
            head_sha=head_sha,
            current_head_sha=head_sha_of(item, current),
            checks=checks,
            checks_expected=checks_expected,
            freshness=freshness,
            failed_logs=logs,
        )

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
            if check.get("conclusion") in FAILED_CONCLUSIONS
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
                and run.get("conclusion") not in FAILED_CONCLUSIONS
            ):
                continue
            jobs = await actions.jobs(item.repository, run_id)
            run_failed_jobs = [
                job
                for job in jobs
                if job.get("conclusion") in FAILED_CONCLUSIONS and int(job.get("id", 0))
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
        selected_jobs = inspected(failed_jobs, lambda job: job[3].get("conclusion"))
        # Bound the initial downloads; exact serialized metadata and escaping
        # are accounted for after the final head/base check by readiness_of.
        tail_bytes = effective_tail_bytes(log_tail_bytes, len(selected_jobs))
        logs: list[dict[str, Any]] = []
        for run_id, run_attempt, artifact_names, job in selected_jobs:
            job_id = int(job["id"])
            tail, truncated = await actions.job_log_tail(
                item.repository, job_id, tail_bytes
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
                    "tail_limit_bytes": tail_bytes,
                    "truncated": truncated,
                }
            )
        return logs

    async def _behind_by(self, item: GitHubItem, base_sha: str, head_sha: str) -> int:
        payload = await self.get(
            f"/repos/{item.repository}/compare/"
            f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        )
        behind_by = payload.get("behind_by") if isinstance(payload, dict) else None
        if not isinstance(behind_by, int) or behind_by < 0:
            raise ServiceError(t("integrations.github.response.response"))
        return behind_by

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
