"""What the ticket service and the code host read and write, whichever
provider answers.

Tickets and pull requests are named by their URLs, and the comments, reviews,
threads, and jobs in them by opaque ids that mean something only together
with the item's URL. A repository is named by its path, which its provider
checks and spells (``owner/repo`` on GitHub, ``group/sub/project`` on GitLab).

Each resource name maps to the schema of one item. A provider translates its
own responses into these shapes, and :meth:`ReadPage.of` holds every item to
its resource's schema, so the bundled inspections and the agents reading the
member CLI see one shape from every provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.runtime.port import SemanticReaction, ServiceError
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

# Room for JSON escaping in CLI, member result, and host envelope.
MAX_PAGE_BYTES = STREAM_READ_LIMIT // 16
MAX_LOG_TAIL_BYTES = MAX_PAGE_BYTES // 10


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _absent(value: object) -> bool:
    """A field that is left out of a result rather than shown empty."""
    return value is None


class ReadinessQuery(ReadModel):
    failed_logs: bool = False
    log_tail_bytes: int = Field(default=MAX_LOG_TAIL_BYTES, ge=1, le=MAX_LOG_TAIL_BYTES)


class DependencyAlertQuery(ReadModel):
    """A page of a repository's alerts, or with ``id`` the one alert alone."""

    state: Literal["open", "resolved", "dismissed"] = "open"
    page_size: int = Field(default=30, ge=1, le=100)
    id: str = Field(default="", max_length=256)


class PageQuery(ReadModel):
    page_size: int = Field(default=30, ge=1, le=100)
    #: The thread whose comments ``review_thread_comments`` reads.
    node: str = Field(default="", max_length=256)


class TicketSearchQuery(ReadModel):
    """Tickets of a tracker, with every whitespace-separated term required."""

    text: str = Field(default="", max_length=256)
    state: Literal["open", "closed", "all"] = "open"
    label: str = Field(default="", max_length=256)
    page_size: int = Field(default=30, ge=1, le=100)


WorkKind = Literal["ticket", "pull_request"]


class WorkTarget(ReadModel):
    """The ticket or pull request a command worked on, in one shape for every
    command: the member CLI records it as the work target of its trace."""

    kind: WorkKind
    url: str
    title: str
    #: The short reference the service shows it by where it is kept (GitHub
    #: ``#12``, a GitLab merge request ``!12``, Jira ``PROJ-123``).
    label: str


Lane = Literal["new", "ready", "in_progress", "done"]
ActorName = NewType("ActorName", str)
"""A public service account name, resolved through the member roster."""


class TicketRelative(WorkTarget):
    """A ticket another ticket is related to, with its state."""

    state: str


class TicketSummary(WorkTarget):
    """One search result, without the ticket's unbounded body or relations."""

    key: str
    state: str
    lane: Lane | None
    labels: list[str]
    updated_at: str


class Ticket(WorkTarget):
    #: What names the ticket in its service, short enough for a branch name
    #: (``ticket/<key>``): ``123``, ``PROJ-123``.
    key: str
    #: Where the ticket is kept: a repository on GitHub, a project on Jira.
    tracker: str
    #: The code host's repository the ticket belongs to; ``None`` in a
    #: service whose tickets belong to none.
    repository: str | None
    body: str
    state: str
    assignees: list[ActorName]
    labels: list[str]
    #: The ticket's lane on the board; ``None`` when it is not on it.
    lane: Lane | None
    #: The ticket this one is a part of, meant to be completed once its
    #: children are closed.
    parent: TicketRelative | None
    children: list[TicketRelative]
    #: The tickets meant to be closed before this one is started.
    blocked_by: list[TicketRelative]
    #: The tickets this one must be closed before.
    blocks: list[TicketRelative]
    #: The ticket's own interactive setting. An ancestor can keep it
    #: interactive even when this is false.
    interactive: bool
    #: URL of the nearest interactive ancestor, if any.
    interactive_ancestor: str | None


class PullRequest(WorkTarget):
    repository: str
    body: str
    state: str
    assignees: list[ActorName]
    author: ActorName
    requested_reviewers: list[ActorName]
    labels: list[str]
    #: ``assistant`` when the member wrote it, as for comments.
    author_type: str
    head: str
    #: The repository the head branch is on.
    head_repository: str
    head_sha: str
    base: str
    merged: bool
    draft: bool
    changed_files: int | None


class Comment(ReadModel):
    id: str
    body: str
    author: ActorName
    author_type: str
    created_at: str | None
    url: str | None


class ThreadComment(Comment):
    reply_to_id: str | None


class Attachment(ReadModel):
    """A file a ticket's body or comments refer to, such as a screenshot."""

    id: str
    #: The name it was attached by; not a path.
    name: str
    media_type: str
    #: Its size in bytes.
    size: int
    #: Where it is referred to: ``body``, or the id of a comment.
    referenced_by: list[str]


class TicketPullRequest(ReadModel):
    """A pull request of the code host that refers to a ticket."""

    url: str
    label: str
    title: str
    state: str
    merged: bool


ReviewVerdict = Literal["approve", "request_changes", "comment"]


class Review(ReadModel):
    id: str
    body: str
    author: ActorName
    author_type: str
    #: What the reviewer decided; ``dismissed`` when the decision was set aside.
    verdict: ReviewVerdict | Literal["dismissed"]
    #: The commit the review was given on.
    commit: str | None
    submitted_at: str | None
    url: str | None


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
    #: ``None`` where the service does not keep it.
    resolved: bool | None
    outdated: bool | None
    comments: list[ThreadComment]
    comments_complete: bool


class Check(ReadModel):
    name: str
    status: str
    conclusion: str | None
    details_url: str
    details: str | None = Field(default=None, exclude_if=_absent)


class CompletionBlocker(ReadModel):
    code: str
    message: str
    next_action: str


class FailedLog(ReadModel):
    #: The job's attempt, as the CI names it: a run and its attempt are in it.
    job_id: str
    name: str
    conclusion: str
    url: str
    artifact_names: list[str]
    log: str
    log_bytes: int
    tail_limit_bytes: int
    truncated: bool


class Readiness(ReadModel):
    """Whether a pull request's head may be reported complete."""

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


class CommentWritten(ReadModel):
    id: str
    url: str | None
    author: str
    created_at: str | None
    target: WorkTarget


class ReactionAdded(ReadModel):
    reaction: SemanticReaction
    #: What was reacted to, by the id its item's read gave it.
    id: str


#: What a resource is part of, which its subject names: a ticket or a pull
#: request by its URL, a repository by its path.
ReadSubject = Literal["ticket", "pull_request", "repository"]

#: Every resource, by what it is part of, with the schema of one of its items.
RESOURCES_BY_SUBJECT: dict[ReadSubject, dict[str, type[ReadModel]]] = {
    "ticket": {
        # Unlike the detail resources, this subject is a tracker path.
        "tracker_tickets": TicketSummary,
        "tickets": Ticket,
        "ticket_comments": Comment,
        "ticket_attachments": Attachment,
        "ticket_pull_requests": TicketPullRequest,
    },
    "pull_request": {
        "pull_requests": PullRequest,
        "pull_request_comments": Comment,
        "pull_request_reviews": Review,
        "pull_request_files": FileChange,
        "pull_request_threads": ReviewThread,
        "review_thread_comments": ThreadComment,
        "pull_request_readiness": Readiness,
    },
    "repository": {"dependency_alerts": DependencyAlert},
}
RESOURCES: dict[str, type[ReadModel]] = {
    name: schema
    for resources in RESOURCES_BY_SUBJECT.values()
    for name, schema in resources.items()
}
RESOURCE_SUBJECTS: dict[str, ReadSubject] = {
    name: subject
    for subject, resources in RESOURCES_BY_SUBJECT.items()
    for name in resources
}
#: The resources the ticket service reads; the code host reads the others,
#: the pull requests of a ticket among them.
TICKET_RESOURCES = frozenset(
    {"tracker_tickets", "tickets", "ticket_comments", "ticket_attachments"}
)
#: The resources that are one item, read without pagination.
DETAIL_RESOURCES = frozenset({"tickets", "pull_requests", "pull_request_readiness"})


class ReadPage(ReadModel):
    """One bounded resource page, with the work target it was read of when
    that is one ticket or pull request."""

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
    ) -> ReadPage:
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
    resource: str, parameters: dict[str, Any] | None, continuation: str
) -> dict[str, Any]:
    """The conditions of a read, checked before any provider is asked.

    dependency_alerts follows DependencyAlertQuery: with an ``id`` it reads
    that one alert, with no other condition and no continuation. Readiness
    accepts failed_logs and log_tail_bytes. tracker_tickets follows
    TicketSearchQuery with a tracker path as its subject. Other collections
    take page_size (1-100), and thread comments also the ``node`` id of their
    thread. One item is read without conditions or continuation.

    Raises:
        ServiceError: If the read is not one a service answers.
    """
    if resource not in RESOURCES:
        raise ServiceError(t("integrations.resources.resource"))
    if continuation and resource in DETAIL_RESOURCES:
        raise ServiceError(t("integrations.resources.continuation"))
    given = {} if parameters is None else parameters
    try:
        if not isinstance(given, dict):
            raise ValueError
        if resource == "tracker_tickets":
            return TicketSearchQuery.model_validate(given).model_dump()
        if resource == "dependency_alerts":
            query = DependencyAlertQuery.model_validate(given)
            if query.id and (query.model_fields_set != {"id"} or continuation):
                raise ValueError
            return query.model_dump()
        if resource == "pull_request_readiness":
            return ReadinessQuery.model_validate(given).model_dump()
        if resource in DETAIL_RESOURCES:
            if given:
                raise ValueError
            return {}
        conditions = PageQuery.model_validate(given).model_dump()
        if bool(conditions["node"]) != (resource == "review_thread_comments"):
            raise ValueError
        return conditions
    except ValueError, ValidationError:
        raise ServiceError(t("integrations.resources.parameters")) from None
