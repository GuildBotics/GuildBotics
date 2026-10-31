"""Whether a pull request's head may be reported complete, from what its code
host says of the head's checks and of the base, whichever code host that is.

A provider reads the pull request, its checks, and the base, then the pull
request again; :func:`readiness_of` judges from those reads alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import MAX_PAGE_BYTES, Readiness, WorkTarget
from guildbotics.utils.i18n_tool import t

#: The conclusions of a check that failed.
FAILED_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "startup_failure",
        "stale",
        "timed_out",
    }
)
#: The failed conclusions that are the failure itself, not its fallout.
_PRIMARY_FAILED_CONCLUSIONS = frozenset(
    {"action_required", "failure", "startup_failure", "timed_out"}
)
_SUCCESS_CONCLUSIONS = {"neutral", "skipped", "success"}


@dataclass(frozen=True)
class Freshness:
    """The base an open pull request's head was compared with."""

    base_sha: str
    behind_by: int
    #: The base's tip when the pull request was read again.
    current_base_sha: str
    #: Whether the host requires this head to include the current base.
    requires_up_to_date: bool


def check_rollup(checks: list[dict[str, Any]]) -> str:
    """One state of all the head's checks."""
    if not checks:
        return "no_checks"
    if any(item.get("conclusion") in FAILED_CONCLUSIONS | {"error"} for item in checks):
        return "failure"
    if any(item.get("status") != "completed" for item in checks):
        return "pending"
    if all(item.get("conclusion") in _SUCCESS_CONCLUSIONS for item in checks):
        return "success"
    return "pending"


def inspected[Job](
    jobs: Sequence[Job], conclusion: Callable[[Job], object]
) -> list[Job]:
    """The failed ``jobs`` whose logs are read: the primary failures, or all
    of them when there is none -- a cancelled or stale job is usually fallout
    from the real failure."""
    primary = [job for job in jobs if conclusion(job) in _PRIMARY_FAILED_CONCLUSIONS]
    return primary or list(jobs)


def effective_tail_bytes(log_tail_bytes: int, jobs: int) -> int:
    """How much of each of ``jobs`` failed logs to download: the page holds
    them all; the exact fit is :func:`readiness_of`'s."""
    return min(log_tail_bytes, max(1, MAX_PAGE_BYTES // jobs))


def readiness_of(
    *,
    target: WorkTarget,
    head_sha: str,
    current_head_sha: str,
    checks: list[dict[str, Any]],
    checks_expected: bool,
    freshness: Freshness | None,
    failed_logs: list[dict[str, Any]] | None,
) -> Readiness:
    """The readiness of the pull request ``target``.

    Args:
        target: The pull request, as it was read last.
        head_sha: The head the checks are of.
        current_head_sha: The head when the pull request was read again.
        checks: The head's checks.
        checks_expected: Whether checks are expected on the head: then none is
            "not registered yet", never "no checks".
        freshness: The base the head was compared with; ``None`` when the
            pull request is not open, and readiness does not apply.
        failed_logs: The failed jobs' logs, when asked for.
    """
    rollup = check_rollup(checks)
    result: dict[str, Any] = {
        "target": target.model_dump(),
        "base_sha": None,
        "head_sha": head_sha,
        "behind_by": None,
        "out_of_date": None,
        "current_base_sha": None,
        "current_head_sha": current_head_sha,
        "rollup": rollup,
        "checks": checks,
        "checks_expected": False,
        "readiness": "not_applicable",
        "completion_blockers": [],
    }
    if freshness is not None:
        blockers = _completion_blockers(
            rollup=rollup,
            checks_expected=checks_expected,
            behind_by=freshness.behind_by,
            requires_up_to_date=freshness.requires_up_to_date,
            changed={
                "head": current_head_sha != head_sha,
                "base": freshness.current_base_sha != freshness.base_sha,
            },
        )
        result.update(
            base_sha=freshness.base_sha,
            behind_by=freshness.behind_by,
            out_of_date=freshness.behind_by > 0,
            current_base_sha=freshness.current_base_sha,
            checks_expected=checks_expected,
            readiness="blocked" if blockers else "ready",
            completion_blockers=blockers,
        )
    if failed_logs is not None:
        result["failed_logs"] = failed_logs
        fit_log_tails(result)
    return Readiness.model_validate(result)


def _completion_blockers(
    *,
    rollup: str,
    checks_expected: bool,
    behind_by: int,
    requires_up_to_date: bool,
    changed: dict[str, bool],
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if requires_up_to_date and behind_by > 0:
        blockers.append(
            {
                "code": "base_out_of_date",
                "message": f"The PR head is {behind_by} commit(s) behind its base.",
                "next_action": "Merge or rebase the base branch, push, and check again.",
            }
        )
    if changed["head"]:
        blockers.append(
            {
                "code": "head_changed",
                "message": "The PR head changed while readiness was being checked.",
                "next_action": "Check the new head SHA and its CI results again.",
            }
        )
    if changed["base"]:
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


def fit_log_tails(result: dict[str, Any]) -> None:
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
