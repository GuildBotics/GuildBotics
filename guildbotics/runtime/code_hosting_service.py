"""The code host a member works with: its repositories, their issues and pull
requests, their CI, and where its git connects.

Reads are open to every command, inside its isolated environment too. The
other operations are the host's: a command's environment reaches them only
through the member commands, so a service there leaves them unavailable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import IO, Any, Literal

from pydantic import Field

from guildbotics.entities import Task
from guildbotics.runtime.code_hosting_resources import (
    Readiness,
    ReadinessQuery,
    ReadModel,
    RepositoryReadPage,
    WorkTarget,
)

ItemKind = Literal["issue", "pull_request"]
ReviewEvent = Literal["approve", "request-changes", "comment"]
ReactionTarget = Literal["issue-comment", "pr-review-comment", "pr-review"]


class ItemRef(ReadModel):
    """Where an issue or pull request URL of this code host points."""

    kind: ItemKind
    owner: str
    repo: str
    number: int
    #: The URL as this code host spells it.
    url: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


class PullRequestHead(ReadModel):
    owner: str
    repo: str
    branch: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


class CommentWritten(ReadModel):
    comment_id: int | None
    html_url: str | None
    author: str
    created_at: str | None
    target: WorkTarget


class IssueCreated(ReadModel):
    issue_number: int
    issue_title: str
    repo: str
    issue_url: str
    labels: list[str]
    target: WorkTarget


class IssueUpdated(ReadModel):
    issue_number: int
    issue_url: str
    repo: str
    title: str
    state: str
    state_changed: bool
    labels: list[str]
    body: str
    target: WorkTarget


class PullRequestCreated(ReadModel):
    pr_number: int
    pr_url: str
    created: bool
    draft: bool
    head: str
    base: str
    target: WorkTarget


class PullRequestUpdated(ReadModel):
    pr_number: int
    pr_url: str
    title: str
    body: str
    target: WorkTarget


class ReviewSubmitted(ReadModel):
    review_id: int | None
    html_url: str | None
    state: str | None
    commit_id: str
    submitted_at: str | None
    target: WorkTarget


class ReviewCommentWritten(ReadModel):
    review_comment_id: int | None
    html_url: str | None
    created_at: str | None
    path: str
    line: int
    side: str
    target: WorkTarget


class ReplyWritten(ReadModel):
    reply_comment_id: int | None
    html_url: str | None
    created_at: str | None
    target: WorkTarget


class ReactionAdded(ReadModel):
    #: The reaction's own id, where the code host gives it one.
    reaction_id: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    content: str
    comment_id: int


class ArtifactRef(ReadModel):
    repo: str
    run_id: int | None
    artifact_id: int
    artifact_name: str


class ClosedItem(ReadModel):
    """An issue or pull request of the board that was closed (or merged)."""

    kind: ItemKind
    repo: str
    number: int
    title: str
    url: str
    #: When it was merged if it was, else when it was closed (ISO 8601).
    closed_at: str
    merged: bool


class CodeHostingService(ABC):
    @abstractmethod
    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        """Read a resource of :data:`~.code_hosting_resources.RESOURCES`;
        never accept URLs or executable queries.

        The conditions are :func:`~.code_hosting_resources.read_conditions`'.
        A continuation belongs to the same provider, repository, resource,
        identifier, and conditions that returned it.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        """How the code host knows the member, as ``member context`` shows it.

        Args:
            check: Confirm with the code host that the member's credential
                is accepted.

        Raises:
            MemberCapabilityError: If ``check`` and the credential is refused.
        """
        raise NotImplementedError

    def locate(self, url: str, kind: ItemKind | None = None) -> ItemRef:
        """Where the issue or pull request ``url`` of this code host is.

        Raises:
            MemberCapabilityError: If ``url`` is not one, or not of ``kind``.
        """
        raise NotImplementedError

    async def comment(
        self,
        url: str,
        body: str,
        *,
        kind: ItemKind | None = None,
        status: dict[str, object] | None = None,
    ) -> CommentWritten:
        """Comment on the issue or pull request ``url``.

        Args:
            url: The issue or pull request.
            body: What the comment says.
            kind: What ``url`` must be.
            status: A workflow status (``workflow_status_fields``) the comment
                carries for the patrol, which also addresses the item's author.
        """
        raise NotImplementedError

    async def create_issue(
        self, repo: str, title: str, body: str, labels: Sequence[str]
    ) -> IssueCreated:
        """Open an issue; labels are the repository's own, never new ones."""
        raise NotImplementedError

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
        """Change an issue; a label is added or removed without touching the
        labels others set meanwhile."""
        raise NotImplementedError

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
        """Open a pull request, or return the open one of the same head and
        base. An empty ``base`` is the repository's default branch."""
        raise NotImplementedError

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        drop_issue_links: bool,
    ) -> PullRequestUpdated:
        """Change a pull request; a new body keeps the issue links of the old
        one unless ``drop_issue_links``."""
        raise NotImplementedError

    async def review(self, url: str, body: str, event: ReviewEvent) -> ReviewSubmitted:
        """Submit a review verdict on the pull request's head."""
        raise NotImplementedError

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
        """Comment on a line (or a range ending at it) of the pull request's diff."""
        raise NotImplementedError

    async def reply(self, url: str, reply_target_id: int, body: str) -> ReplyWritten:
        """Reply in the review thread of the pull request rooted at the comment."""
        raise NotImplementedError

    async def add_reaction(
        self,
        repo: str,
        target: ReactionTarget,
        comment_id: int,
        reaction: str,
        pr_number: int | None = None,
    ) -> ReactionAdded:
        """React to a comment, a review comment, or a review (of ``pr_number``)."""
        raise NotImplementedError

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        """The pull request's CI and whether its head may be reported complete."""
        raise NotImplementedError

    async def linked_pull_requests(self, issue_url: str) -> list[str]:
        """The pull requests that refer to the issue."""
        raise NotImplementedError

    async def open_pull_requests(self, remote_url: str, branch: str) -> list[str]:
        """The open pull requests whose head is ``branch`` of the remote."""
        raise NotImplementedError

    def artifact(
        self, url: str, name: str, limit: int
    ) -> AbstractAsyncContextManager[tuple[ArtifactRef, IO[bytes]]]:
        """The newest CI artifact ``name`` of a run, or of a pull request's
        head, as an archive of at most ``limit`` bytes."""
        raise NotImplementedError

    async def clone_url(self, owner: str, repo: str) -> str:
        """Where git fetches and pushes the repository."""
        raise NotImplementedError

    async def default_branch(self, owner: str, repo: str) -> str:
        raise NotImplementedError

    async def pull_request_head(self, url: str) -> PullRequestHead:
        """The repository and branch the pull request's head lives on."""
        raise NotImplementedError

    async def push_credential(self) -> str:
        """The credential git authenticates with as the member; empty for none."""
        raise NotImplementedError

    def commit_url(self, remote_url: str, sha: str) -> str:
        """Where the commit is shown, if the remote is this code host's."""
        raise NotImplementedError

    def repository_from_remote(self, remote_url: str) -> tuple[str, str] | None:
        """The ``(owner, repo)`` a remote names on this code host."""
        raise NotImplementedError

    def remote_host(self, remote_url: str) -> str:
        """The host a remote names, without a credential its URL may carry."""
        raise NotImplementedError

    async def pull_request_candidates(self) -> list[Task]:
        """The open pull requests that ask something of the member, in patrol
        order. One past the automatic re-review limit is handed to a human
        instead."""
        raise NotImplementedError

    async def refresh_pull_request(self, task: Task) -> Task | None:
        """Re-read a pull request candidate immediately before it is dispatched."""
        raise NotImplementedError
