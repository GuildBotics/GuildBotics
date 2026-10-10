"""What a code host's repository reads return, whichever provider answers.

Each resource name maps to the schema of one item. A provider translates its
own responses into these shapes, and :meth:`RepositoryReadPage.of` holds every
item to its resource's schema, so the bundled inspections and the agents
reading the member CLI see one shape from every provider.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

# Room for JSON escaping in CLI, member result, and host envelope.
MAX_PAGE_BYTES = STREAM_READ_LIMIT // 16
MAX_LOG_TAIL_BYTES = MAX_PAGE_BYTES // 10


class RepositoryReadError(RuntimeError):
    """A safe repository read failure, without upstream bodies or credentials."""


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _absent(value: object) -> bool:
    """A field that is left out of a result rather than shown empty."""
    return value is None


class ReadinessQuery(ReadModel):
    failed_logs: bool = False
    log_tail_bytes: int = Field(default=MAX_LOG_TAIL_BYTES, ge=1, le=MAX_LOG_TAIL_BYTES)


class DependencyAlertQuery(ReadModel):
    state: Literal["open", "resolved", "dismissed"] = "open"
    page_size: int = Field(default=30, ge=1, le=100)


class PageQuery(ReadModel):
    page_size: int = Field(default=30, ge=1, le=100)
    node: str = Field(default="", max_length=256)


class WorkTarget(ReadModel):
    """The issue or pull request a command worked on, in one shape for every
    command: the member CLI records it as the work target of its trace."""

    kind: Literal["issue", "pull_request"]
    repo: str
    number: int
    title: str
    html_url: str


class Issue(WorkTarget):
    body: str
    state: str
    assignees: list[str]
    labels: list[str]


class PullRequest(Issue):
    #: ``assistant`` when the member wrote it, as for comments.
    author_type: str
    head: str
    head_repo: str
    head_owner: str
    head_repo_name: str
    head_sha: str
    base: str
    merged: bool
    draft: bool
    changed_files: int | None


class Comment(ReadModel):
    id: int | None
    body: str
    author: str
    author_type: str
    created_at: str | None
    html_url: str | None


class ThreadComment(Comment):
    reply_to_id: int | None


class PullRequestLink(ReadModel):
    repo: str
    number: int


class TimelineEvent(ReadModel):
    event: str | None
    pull_request: PullRequestLink | None


class ProjectField(ReadModel):
    field: str
    value: str | int | float | None


class ProjectItem(ReadModel):
    item_id: str | None
    project_title: str | None
    project_number: int | None
    project_url: str | None
    fields: list[ProjectField]


class Review(ReadModel):
    id: int | None
    body: str
    author: str
    author_type: str
    state: str
    submitted_at: str | None
    html_url: str | None
    commit_id: str | None


class CommentableLine(ReadModel):
    line: int
    side: Literal["LEFT", "RIGHT"]
    #: The line on each side, for a side the line is on.
    left_line: int | None = Field(default=None, exclude_if=_absent)
    right_line: int | None = Field(default=None, exclude_if=_absent)


class FileChange(ReadModel):
    path: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None
    patch_available: bool
    patch_complete: bool
    commentable_lines: list[CommentableLine]


class ReviewThread(ReadModel):
    id: str
    resolved: bool
    outdated: bool
    comments: list[ThreadComment]
    comments_complete: bool


class Check(ReadModel):
    name: str
    status: str
    conclusion: str | None
    details_url: str
    source: str


class CompletionBlocker(ReadModel):
    code: str
    message: str
    next_action: str


class FailedLog(ReadModel):
    run_id: int
    run_attempt: int
    job_id: int
    name: str
    conclusion: str
    html_url: str
    artifact_names: list[str]
    log: str
    log_bytes: int
    tail_limit_bytes: int
    truncated: bool


class Readiness(ReadModel):
    """Whether a pull request's head may be reported complete."""

    repo: str
    pr_number: int
    pr_url: str
    target: WorkTarget
    base_sha: str | None
    head_sha: str
    behind_by: int | None
    out_of_date: bool | None
    current_base_sha: str | None
    current_head_sha: str
    rollup: Literal["success", "failure", "pending", "no_checks"]
    checks: list[Check]
    checks_expected: bool
    readiness: Literal["ready", "blocked", "not_applicable"]
    completion_blockers: list[CompletionBlocker]
    #: Present only when failed logs were asked for.
    failed_logs: list[FailedLog] | None = Field(default=None, exclude_if=_absent)


class AdvisoryIdentifier(ReadModel):
    type: str
    value: str


class DependencyAlert(ReadModel):
    """A dependency vulnerability; identifiers and repository names are opaque."""

    id: str
    state: Literal["open", "resolved", "dismissed"]
    url: str | None = None
    package: str | None = None
    ecosystem: str | None = None
    manifest_path: str | None = None
    severity: str | None = None
    identifiers: list[AdvisoryIdentifier] = Field(default_factory=list)
    summary: str | None = None
    description: str | None = None
    affected_versions: str | None = None
    patched_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


#: Every resource a code host reads, with the schema of one of its items.
RESOURCES: dict[str, type[ReadModel]] = {
    "issues": Issue,
    "pull_requests": PullRequest,
    "issue_comments": Comment,
    "issue_timeline": TimelineEvent,
    "issue_projects": ProjectItem,
    "pull_request_reviews": Review,
    "pull_request_files": FileChange,
    "pull_request_threads": ReviewThread,
    "review_thread_comments": ThreadComment,
    "pull_request_readiness": Readiness,
    "dependency_alerts": DependencyAlert,
}
#: The resources that are one item, read without pagination.
DETAIL_RESOURCES = frozenset({"issues", "pull_requests", "pull_request_readiness"})


class RepositoryReadPage(ReadModel):
    """One bounded resource page, with a host-observed work target when present."""

    items: list[dict[str, Any]]
    continuation: str | None = None
    target: WorkTarget | None = None

    @classmethod
    def of(
        cls,
        resource: str,
        items: Sequence[ReadModel | dict[str, Any]],
        *,
        continuation: str | None = None,
        target: WorkTarget | None = None,
    ) -> RepositoryReadPage:
        """A page of ``resource``, each item held to its schema.

        Raises:
            pydantic.ValidationError: If an item is not of that shape.
        """
        schema = RESOURCES[resource]
        return cls(
            items=[
                schema.model_validate(
                    item.model_dump() if isinstance(item, ReadModel) else item
                ).model_dump()
                for item in items
            ],
            continuation=continuation,
            target=target,
        )


def read_conditions(
    resource: str, repo: str, identifier: str, parameters: dict[str, Any] | None
) -> dict[str, Any]:
    """The conditions of a read, checked before any provider is asked.

    dependency_alerts follows DependencyAlertQuery (no conditions with an
    identifier). Every other resource requires its number as identifier;
    collections take page_size (1-100), and thread comments also take the
    node identifier of their thread. Readiness accepts failed_logs and
    log_tail_bytes.

    Raises:
        RepositoryReadError: If the read is not one a code host answers.
    """
    if resource not in RESOURCES:
        raise RepositoryReadError(t("integrations.repository.resource"))
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo
    ) or repo.rsplit("/", maxsplit=1)[-1] in {".", ".."}:
        raise RepositoryReadError(t("integrations.repository.repository"))
    if identifier and not re.fullmatch(r"[1-9][0-9]{0,19}", identifier):
        raise RepositoryReadError(t("integrations.repository.identifier"))
    if resource != "dependency_alerts" and not identifier:
        raise RepositoryReadError(t("integrations.repository.identifier"))
    try:
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError
        if resource == "dependency_alerts":
            if not identifier:
                return DependencyAlertQuery.model_validate(
                    parameters or {}
                ).model_dump()
        elif resource == "pull_request_readiness":
            return ReadinessQuery.model_validate(parameters or {}).model_dump()
        elif resource not in DETAIL_RESOURCES:
            conditions = PageQuery.model_validate(parameters or {}).model_dump()
            if resource == "review_thread_comments":
                if not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", conditions["node"]):
                    raise ValueError
            elif conditions["node"]:
                raise ValueError
            return conditions
        if parameters not in (None, {}):
            raise ValueError
        return {}
    except ValueError:
        raise RepositoryReadError(t("integrations.repository.parameters")) from None
