"""The local code host: repositories whose pull requests are files of the
workspace (:mod:`.store`), and whose remote is a bare repository.

A pull request's CI is one job, ``local``, run once (attempt 1) on every
head. How it ends is the project's setting ``services.code_hosting_service.checks``:
``success`` (the default), ``failure``, or ``pending``, which leaves it running
until a conclusion is recorded for the head (:func:`.store.finish_checks`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.diff_lines import file_change
from guildbotics.integrations.local import items, store
from guildbotics.integrations.pull_request_patrol import (
    FEEDBACK,
    REVIEW,
    Comment,
    PullRequest,
    Review,
    ReviewThread,
    patrol,
    pull_request_work,
)
from guildbotics.integrations.readiness import Freshness, readiness_of
from guildbotics.integrations.repository_scope import check_path
from guildbotics.runtime.code_hosting_service import (
    ArtifactRef,
    CodeHostingService,
    PullRequestCreated,
    PullRequestHead,
    PullRequestUpdated,
    ReplyWritten,
    ReviewCommentWritten,
    ReviewSubmitted,
    TicketLink,
)
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    CommentWritten,
    ReactionAdded,
    Readiness,
    ReadinessQuery,
    ReadPage,
    ReviewVerdict,
    read_conditions,
)
from guildbotics.utils.i18n_tool import t

#: The one job of a head's CI, and its one attempt.
_JOB = "local"
_ATTEMPT = 1


class LocalCodeHostingService(CodeHostingService):
    def __init__(self, person: Person, team: Team) -> None:
        config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.person = person
        #: The namespace the member writes in.
        self.scope = str(config.get("owner") or "")
        self._checks = str(config.get("checks") or "success")

    async def aclose(self) -> None:
        """The files are opened per operation."""

    # -- reads --------------------------------------------------------------

    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        conditions = read_conditions(resource, parameters, continuation)
        if resource in TICKET_RESOURCES:
            raise ServiceError(t("integrations.resources.resource"))
        if resource == "dependency_alerts":
            raise ServiceUnsupported("The local code host keeps no dependency alerts.")
        if resource == "ticket_pull_requests":
            return items.page(
                resource,
                await self._ticket_pull_requests(subject),
                conditions,
                continuation,
            )
        ref = store.locate(subject, "pull_request")
        item = store.load(ref)
        if resource == "pull_request_readiness":
            readiness = await self.readiness(subject, **conditions)
            return ReadPage.of(resource, [readiness], target=readiness.target)
        if resource == "pull_requests":
            return ReadPage.of(
                resource, [self._detail(ref, item)], target=items.target(ref, item)
            )
        return items.page(
            resource,
            self._entries(resource, ref, item, conditions),
            conditions,
            continuation,
        )

    async def _ticket_pull_requests(self, ticket_url: str) -> list[dict[str, Any]]:
        ticket = _local_ticket(ticket_url)
        if ticket is None:
            raise ServiceUnsupported(
                f"The local code host knows only local tickets, not {ticket_url}."
            )
        store.load(ticket)
        return [
            {
                "url": ref.url,
                "label": ref.label,
                "title": pull["title"],
                "state": pull["state"],
                "merged": pull["merged"],
            }
            for ref, pull in store.items("pull_request")
            if any(link["url"] == ticket.url for link in pull["tickets"])
        ]

    def _detail(self, ref: store.Ref, item: dict[str, Any]) -> dict[str, Any]:
        return {
            **items.target(ref, item).model_dump(),
            "repository": ref.place,
            "body": item["body"],
            "state": item["state"],
            "assignees": item["assignees"],
            "labels": item["labels"],
            "author_type": self._author_type(item["author"]),
            "head": item["head"],
            "head_repository": item["head_repository"],
            "head_sha": _head_sha(item),
            "base": item["base"],
            "merged": item["merged"],
            "draft": item["draft"],
            "changed_files": None,
        }

    def _entries(
        self,
        resource: str,
        ref: store.Ref,
        item: dict[str, Any],
        conditions: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if resource == "pull_request_comments":
            return [self._comment(comment) for comment in item["comments"]]
        if resource == "pull_request_reviews":
            return [
                {
                    "id": review["id"],
                    "body": review["body"],
                    "author": review["author"],
                    "author_type": self._author_type(review["author"]),
                    "verdict": review["verdict"],
                    "commit": review["commit"],
                    "submitted_at": review["created_at"],
                    "url": None,
                }
                for review in item["reviews"]
            ]
        if resource == "pull_request_files":
            return _files(ref.place, item)
        threads = _threads(item)
        if resource == "pull_request_threads":
            return [
                {
                    "id": root["id"],
                    # Kept by no local file.
                    "resolved": None,
                    "outdated": None,
                    "comments": [self._thread_comment(c) for c in comments],
                    "comments_complete": True,
                }
                for root, comments in threads
            ]
        thread = next(
            (
                comments
                for root, comments in threads
                if root["id"] == conditions["node"]
            ),
            None,
        )
        if thread is None:
            raise ServiceNotFound(f"'{conditions['node']}' is no thread of {ref.url}.")
        return [self._thread_comment(comment) for comment in thread]

    def _comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": comment["id"],
            "body": comment["body"],
            "author": comment["author"],
            "author_type": self._author_type(comment["author"]),
            "created_at": comment["created_at"],
            "url": None,
        }

    def _thread_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        return {**self._comment(comment), "reply_to_id": comment["in_reply_to"]}

    def _author_type(self, author: str) -> str:
        return Message.ASSISTANT if author == self.person.person_id else Message.USER

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        return {}

    # -- pull requests ------------------------------------------------------

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        ref = store.locate(url, "pull_request")
        return items.comment(self.scope, ref, self.person.person_id, body, status)

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
        base = base.strip() or await self.default_branch(repository)
        found = next(
            (
                (ref, pull)
                for ref, pull in store.items("pull_request", repository)
                if pull["state"] == "open"
                and (pull["head"], pull["base"]) == (head, base)
            ),
            None,
        )
        if found is None:
            links = [_linked(ticket, repository) for ticket in tickets]
            ref, number = store.new_ref("pull_request", repository)
            item = {
                **items.new_item(number, title, body, self.person.person_id),
                "head": head,
                "head_repository": repository,
                "base": base,
                "draft": draft,
                "merged": False,
                "reviewers": [],
                "reviews": [],
                "review_comments": [],
                "tickets": links,
            }
            store.save(self.scope, ref, item)
        else:
            ref, item = found
        return PullRequestCreated(
            created=found is None,
            draft=item["draft"],
            head=head,
            base=base,
            target=items.target(ref, item),
        )

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        tickets: Sequence[TicketLink] | None,
    ) -> PullRequestUpdated:
        ref = store.locate(url, "pull_request")
        item = store.load(ref)
        if tickets is not None:
            item["tickets"] = [_linked(ticket, ref.place) for ticket in tickets]
        for key, value in (("body", body), ("title", title)):
            if value is not None:
                item[key] = value
        store.save(self.scope, ref, item)
        return PullRequestUpdated(body=item["body"], target=items.target(ref, item))

    async def review(
        self, url: str, body: str, verdict: ReviewVerdict, head: str
    ) -> ReviewSubmitted:
        ref, item = _at_head(url, head)
        review = items.entry(
            item, "review", self.person.person_id, body, verdict=verdict, commit=head
        )
        item["reviews"].append(review)
        # A review answers the member's review request, as on every code host.
        item["reviewers"] = [r for r in item["reviewers"] if r != self.person.person_id]
        store.save(self.scope, ref, item)
        return ReviewSubmitted(
            verdict=verdict, commit=head, url=None, target=items.target(ref, item)
        )

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
        ref, item = _at_head(url, head)
        comment = items.entry(
            item,
            "inline",
            self.person.person_id,
            body,
            path=path,
            line=line,
            side=side,
            start_line=start_line,
            start_side=start_side,
            commit=head,
            in_reply_to=None,
        )
        item["review_comments"].append(comment)
        store.save(self.scope, ref, item)
        return ReviewCommentWritten(
            id=comment["id"],
            url=None,
            created_at=comment["created_at"],
            path=path,
            line=line,
            side=side,
            target=items.target(ref, item),
        )

    async def reply(self, url: str, reply_to: str, body: str) -> ReplyWritten:
        ref = store.locate(url, "pull_request")
        item = store.load(ref)
        root = next(
            (
                comment
                for comment in item["review_comments"]
                if comment["id"] == reply_to and comment["in_reply_to"] is None
            ),
            None,
        )
        if root is None:
            raise ServiceNotFound(f"'{reply_to}' roots no review thread of {url}.")
        reply = items.entry(
            item,
            "inline",
            self.person.person_id,
            body,
            **{key: root[key] for key in ("path", "line", "side", "commit")},
            in_reply_to=reply_to,
        )
        item["review_comments"].append(reply)
        store.save(self.scope, ref, item)
        return ReplyWritten(
            id=reply["id"],
            url=None,
            created_at=reply["created_at"],
            target=items.target(ref, item),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        ref = store.locate(url, "pull_request")
        return items.react(self.scope, ref, self.person.person_id, target_id, reaction)

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        ReadinessQuery(failed_logs=failed_logs, log_tail_bytes=log_tail_bytes)
        ref = store.locate(url, "pull_request")
        item = store.load(ref)
        head_sha = _head_sha(item)
        if not head_sha:
            raise ServiceError(f"{url} has no head commit.")
        conclusion = store.checks(ref.place, head_sha) or self._checks
        running = conclusion == "pending"
        freshness = None
        if item["state"] == "open":
            base_sha = store.branch_tip(ref.place, item["base"])
            if not base_sha:
                raise ServiceError(
                    f"The base branch '{item['base']}' of {url} is gone."
                )
            behind_by = store.git(
                ref.place, "rev-list", "--count", f"{head_sha}..{base_sha}"
            )
            freshness = Freshness(
                base_sha, int(behind_by), base_sha, requires_up_to_date=True
            )
        failed = {
            "job_id": f"{_JOB}@{_ATTEMPT}",
            "name": _JOB,
            "conclusion": conclusion,
            "url": "",
            "artifact_names": store.artifacts(ref.place, head_sha),
            "log": "",
            "log_bytes": 0,
            "tail_limit_bytes": log_tail_bytes,
            "truncated": False,
        }
        return readiness_of(
            target=items.target(ref, item),
            head_sha=head_sha,
            current_head_sha=head_sha,
            checks=[
                {
                    "name": _JOB,
                    "status": "in_progress" if running else "completed",
                    "conclusion": None if running else conclusion,
                    "details_url": "",
                }
            ],
            checks_expected=False,
            freshness=freshness,
            failed_logs=(
                ([failed] if conclusion == "failure" else []) if failed_logs else None
            ),
        )

    async def open_pull_requests(self, repository: str, branch: str) -> list[str]:
        return [
            ref.url
            for ref, pull in store.items("pull_request", repository)
            if pull["state"] == "open" and pull["head"] == branch
        ]

    @asynccontextmanager
    async def artifact(
        self, url: str, name: str, limit: int
    ) -> AsyncIterator[tuple[ArtifactRef, IO[bytes]]]:
        ref = store.locate(url, "pull_request")
        head_sha = _head_sha(store.load(ref))
        path = store.artifact(ref.place, head_sha, name)
        if not head_sha or not path.is_file():
            raise ServiceNotFound(f"The head of {url} has no artifact '{name}'.")
        if path.stat().st_size > limit:
            raise ServiceError(f"Artifact '{name}' is above the {limit} byte limit.")
        with path.open("rb") as archive:
            yield ArtifactRef(name=name, id=f"{head_sha}/{name}"), archive

    # -- the git remote -----------------------------------------------------

    async def clone_url(self, repository: str) -> str:
        store.git(repository, "rev-parse", "--git-dir")
        return store.bare(repository).as_uri()

    async def default_branch(self, repository: str) -> str:
        return store.git(repository, "symbolic-ref", "--short", "HEAD")

    async def pull_request_head(self, url: str) -> PullRequestHead:
        item = store.load(store.locate(url, "pull_request"))
        return PullRequestHead(repository=item["head_repository"], branch=item["head"])

    async def git_credential(self) -> None:
        return None

    def check_push(self, repository: str) -> None:
        check_path(self.scope, repository, provider="local")

    def commit_url(self, repository: str, sha: str) -> str:
        return ""

    def repository_from_remote(self, remote_url: str) -> str | None:
        parsed = urlparse(remote_url)
        if parsed.scheme != "file":
            return None
        path = Path(url2pathname(parsed.path))
        repository = unquote(path.parent.name)
        if path.name != "repository.git" or store.bare(repository) != path:
            return None
        return repository

    # -- the pull request patrol --------------------------------------------

    async def pull_request_candidates(self) -> list[Task]:
        return [
            task
            for ref, pull in store.items("pull_request")
            if (task := await self._patrol(ref, pull)) is not None
        ]

    async def refresh_pull_request(self, task: Task) -> Task | None:
        assert task.pull_request_url
        ref = store.locate(task.pull_request_url, "pull_request")
        return await self._patrol(ref, store.load(ref))

    async def can_receive_work(self, url: str) -> bool:
        ref = store.locate(url, "pull_request")
        return pull_request_work(
            _snapshot(ref, store.load(ref)), self.person.person_id
        ) in {FEEDBACK, REVIEW}

    async def _patrol(self, ref: store.Ref, item: dict[str, Any]) -> Task | None:
        async def draft() -> None:
            pull = store.load(ref)
            pull["draft"] = True
            store.save(self.scope, ref, pull)

        return await patrol(
            self,
            _snapshot(ref, item),
            self.person.person_id,
            provider="local",
            person_id=self.person.person_id,
            draft=draft,
        )


def _snapshot(ref: store.Ref, item: dict[str, Any]) -> PullRequest:
    """The pull request as the patrol sees it. An inline comment is a review
    of the commit it is on: a root one stands alone, a reply only answers."""
    reviews = [
        Review(
            author=review["author"],
            body=review["body"],
            submitted_at=review["created_at"],
            commit_oid=review["commit"],
            reply_only=False,
            reactors=_reactors(review),
        )
        for review in item["reviews"]
    ] + [
        Review(
            author=comment["author"],
            body="",
            submitted_at=comment["created_at"],
            commit_oid=comment["commit"],
            reply_only=comment["in_reply_to"] is not None,
            reactors=frozenset(),
        )
        for comment in item["review_comments"]
    ]
    return PullRequest(
        id=ref.url,
        label=ref.label,
        url=ref.url,
        title=item["title"],
        body=item["body"],
        open=item["state"] == "open",
        is_draft=item["draft"],
        created_at=item["created_at"],
        author=item["author"],
        head_oid=_head_sha(item),
        # No human marks a local pull request ready for review.
        ready_at="",
        requested_reviewers=frozenset(item["reviewers"]),
        reviews=tuple(sorted(reviews, key=lambda review: review.submitted_at)),
        comments=tuple(
            Comment(
                author=comment["author"],
                created_at=comment["created_at"],
                reactors=_reactors(comment),
                status=WorkflowStatus.from_payload(comment.get("status")),
            )
            for comment in item["comments"]
        ),
        threads=tuple(
            ReviewThread(
                # No local file keeps a thread resolved.
                resolved=False,
                participants=frozenset(comment["author"] for comment in comments),
                last_author=comments[-1]["author"],
                last_created_at=comments[-1]["created_at"],
                last_reactors=_reactors(comments[-1]),
            )
            for _, comments in _threads(item)
        ),
    )


def _reactors(entry: dict[str, Any]) -> frozenset[str]:
    return frozenset(reaction["author"] for reaction in entry.get("reactions", []))


def _local_ticket(url: str) -> store.Ref | None:
    """The local ticket ``url`` names; ``None`` for another service's URL.

    Raises:
        ServiceNotFound: If it is a local URL that names no ticket.
    """
    if urlparse(url).scheme != store.SCHEME:
        return None
    return store.locate(url, "ticket")


def _linked(ticket: TicketLink, repository: str) -> dict[str, Any]:
    """The link to ``ticket`` a new pull request of ``repository`` keeps. A
    merge closes only a ticket of the repository's own.

    Raises:
        ServiceNotFound: If it is a local ticket that is not there, or a local
            URL of no ticket.
        ServiceUnsupported: If it is another repository's or another
            service's ticket, to be closed.
    """
    local = _local_ticket(ticket.url)
    if local is not None:
        store.load(local)
    if ticket.closes and (
        local is None or store.tracker(local.place).get("repository") != repository
    ):
        raise ServiceUnsupported(
            f"A merge into {repository} closes only a ticket of it, not {ticket.url}."
        )
    return {"url": ticket.url if local is None else local.url, "closes": ticket.closes}


def _at_head(url: str, head: str) -> tuple[store.Ref, dict[str, Any]]:
    """The pull request, if its head is still ``head``.

    Raises:
        ServiceError: If it is not.
    """
    ref = store.locate(url, "pull_request")
    item = store.load(ref)
    current = _head_sha(item)
    if current != head:
        raise ServiceError(
            f"The head of {url} is now {current or 'gone'}, not {head}: read the"
            " new commits, then submit again on that head."
        )
    return ref, item


def _threads(item: dict[str, Any]) -> list[tuple[dict, list[dict]]]:
    comments = item["review_comments"]
    return [
        (root, [root, *(c for c in comments if c["in_reply_to"] == root["id"])])
        for root in comments
        if root["in_reply_to"] is None
    ]


def _head_sha(item: dict[str, Any]) -> str:
    """The commit the pull request's head branch is at; empty if it is gone."""
    return store.branch_tip(item["head_repository"], item["head"])


def _files(repository: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    """What the head changes since it left the base, a file at a time."""
    base, head = store.branch_tip(repository, item["base"]), _head_sha(item)
    if not base or not head:
        raise ServiceError(f"{repository} has no diff of {item['head']}.")
    span = f"{base}...{head}"
    listed = store.git(repository, "diff", "--name-status", "-z", "--no-renames", span)
    names = listed.split("\0")
    statuses = {"A": "added", "D": "removed", "M": "modified"}
    changes = []
    for status, path in zip(names[::2], names[1::2], strict=False):
        diff = store.git(
            repository, "diff", "--no-color", span, "--", path, errors="replace"
        )
        hunks = diff[diff.find("\n@@") + 1 :] if "\n@@" in diff else None
        lines = (hunks or "").splitlines()
        added = sum(line.startswith("+") for line in lines)
        deleted = sum(line.startswith("-") for line in lines)
        changes.append(
            file_change(
                path,
                statuses.get(status, "changed"),
                hunks,
                additions=added,
                deletions=deleted,
                changes=added + deleted,
            )
        )
    return changes
