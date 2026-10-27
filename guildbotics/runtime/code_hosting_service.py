"""The code host a member works with: its repositories, their pull requests,
their CI, and where its git connects.

Reads are open to every command, inside its isolated environment too. The
other operations are the host's: a command's environment reaches them only
through the member commands, so a service there leaves them unavailable.

Every operation fails with :class:`~guildbotics.runtime.port.ServiceError`:
:class:`~guildbotics.runtime.port.ServiceNotFound` when what it names is not
there or not this code host's, and
:class:`~guildbotics.runtime.port.ServiceUnsupported` when the code host
cannot do it at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import IO, Any

from guildbotics.entities import Task
from guildbotics.runtime.port import SemanticReaction, WorkflowStatus
from guildbotics.runtime.resources import (
    CommentWritten,
    ReactionAdded,
    Readiness,
    ReadinessQuery,
    ReadModel,
    ReadPage,
    ReviewVerdict,
    WorkTarget,
)


@dataclass(frozen=True)
class GitCredential:
    """The username and password host git uses for fetch and push."""

    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class TicketLink:
    """A ticket a pull request refers to, and whether merging the pull request
    closes it."""

    url: str
    closes: bool


class PullRequestHead(ReadModel):
    #: The repository the head branch is on.
    repository: str
    branch: str


class PullRequestCreated(ReadModel):
    created: bool
    draft: bool
    head: str
    base: str
    target: WorkTarget


class PullRequestUpdated(ReadModel):
    body: str
    target: WorkTarget


class ReviewSubmitted(ReadModel):
    verdict: ReviewVerdict
    #: The commit the verdict was given on.
    commit: str
    url: str | None
    target: WorkTarget


class ReviewCommentWritten(ReadModel):
    id: str
    url: str | None
    created_at: str | None
    path: str
    line: int
    side: str
    target: WorkTarget


class ReplyWritten(ReadModel):
    id: str
    url: str | None
    created_at: str | None
    target: WorkTarget


class ArtifactRef(ReadModel):
    name: str
    id: str


class CodeHostingService(ABC):
    @abstractmethod
    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        """Read a resource of :data:`~.resources.RESOURCES` other than the
        ticket service's; never accept executable queries.

        Args:
            resource: What to read.
            subject: What it is part of: the pull request's or the ticket's
                URL, or the repository's path.
            parameters: The conditions of
                :func:`~.resources.read_conditions`.
            continuation: What the previous page of the same subject,
                resource, and conditions returned.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        """How the code host knows the member, as ``member context`` shows it.

        Args:
            check: Confirm with the code host that the member's credential
                is accepted.
        """
        raise NotImplementedError

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        """Comment on the pull request ``url``.

        Args:
            url: The pull request.
            body: What the comment says.
            status: The workflow status the comment carries for the patrol,
                which also addresses the pull request's author.
        """
        raise NotImplementedError

    async def create_pull_request(
        self,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
        *,
        draft: bool,
        tickets: Sequence[TicketLink],
    ) -> PullRequestCreated:
        """Open a pull request, or return the open one of the same head and
        base. An empty ``base`` is the repository's default branch.

        Args:
            tickets: The tickets the pull request refers to; a code host that
                cannot link one of them to be closed by a merge raises
                ``ServiceUnsupported`` before anything is written.
        """
        raise NotImplementedError

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        tickets: Sequence[TicketLink] | None,
    ) -> PullRequestUpdated:
        """Change a pull request and optionally replace its ticket links.

        ``tickets=None`` keeps existing links; a sequence replaces all links
        (an empty sequence removes them). A link the host cannot close on
        merge raises ``ServiceUnsupported`` before anything is written.
        """
        raise NotImplementedError

    async def review(
        self, url: str, body: str, verdict: ReviewVerdict, head: str
    ) -> ReviewSubmitted:
        """Submit a review verdict on the pull request's head ``head``.

        Raises:
            ServiceError: If the head is no longer ``head``: the verdict would
                be on commits the member has not read.
        """
        raise NotImplementedError

    async def review_comment(
        self,
        url: str,
        body: str,
        head: str,
        path: str,
        line: int,
        side: str,
        start_line: int | None,
        start_side: str | None,
    ) -> ReviewCommentWritten:
        """Comment on a line (or a range ending at it) of the diff of the pull
        request's head ``head``.

        Raises:
            ServiceError: If the head is no longer ``head``.
        """
        raise NotImplementedError

    async def reply(self, url: str, reply_to: str, body: str) -> ReplyWritten:
        """Reply in the review thread of the pull request rooted at the comment
        ``reply_to``."""
        raise NotImplementedError

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        """React to what the pull request's reads named ``target_id``: a
        conversation comment, an inline comment, or a review's body."""
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

    async def open_pull_requests(self, repository: str, branch: str) -> list[str]:
        """The open pull requests whose head is ``branch`` of the repository."""
        raise NotImplementedError

    def artifact(
        self, url: str, name: str, limit: int
    ) -> AbstractAsyncContextManager[tuple[ArtifactRef, IO[bytes]]]:
        """The CI artifact ``name`` of the pull request's current head, as an
        archive of at most ``limit`` bytes.

        Raises:
            ServiceNotFound: If the head has no such artifact.
        """
        raise NotImplementedError

    async def clone_url(self, repository: str) -> str:
        """Where git fetches and pushes the repository."""
        raise NotImplementedError

    async def default_branch(self, repository: str) -> str:
        raise NotImplementedError

    async def pull_request_head(self, url: str) -> PullRequestHead:
        """The repository and branch the pull request's head lives on."""
        raise NotImplementedError

    async def git_credential(self) -> GitCredential | None:
        """The credential git authenticates with as the member; None for none.

        It is asked right before git connects to the code host, so a provider
        that waits for a host unreachable for a moment does so here.
        """
        raise NotImplementedError

    def check_push(self, repository: str) -> None:
        """Refuse a push to a repository the member does not write to.

        Raises:
            ServiceError: If the repository is outside what the code host is
                configured to write.
        """
        raise NotImplementedError

    def commit_url(self, repository: str, sha: str) -> str:
        """Where the commit of the repository is shown; empty for nowhere."""
        raise NotImplementedError

    def repository_from_remote(self, remote_url: str) -> str | None:
        """The repository a git remote names on this code host."""
        raise NotImplementedError

    async def pull_request_candidates(self) -> list[Task]:
        """The open pull requests that ask something of the member, in patrol
        order. One past the automatic re-review limit is handed to a human
        instead."""
        raise NotImplementedError

    async def refresh_pull_request(self, task: Task) -> Task | None:
        """Re-read a pull request candidate immediately before it is dispatched."""
        raise NotImplementedError

    async def can_receive_work(self, url: str) -> bool:
        """Whether the current conversation dispatches this PR to the member,
        without drafting it or recording a patrol action."""
        raise NotImplementedError
