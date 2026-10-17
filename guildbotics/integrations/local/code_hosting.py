"""The local code host: repositories whose pull requests are files of the
workspace (:mod:`.store`), and whose remote is a bare repository.

Whether a pull request's CI passes is the project's setting
``services.code_hosting_service.checks`` (``success``, the default, or
``failure``): its one job, ``local``, runs once (attempt 1) on every head.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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

_FEEDBACK = "pull_request_feedback"
_REVIEW = "pull_request_review"
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
            if pull["ticket"] == ticket.url
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
        ticket_url: str,
        closes: bool,
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
            ticket = _linked(ticket_url, repository, closes)
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
                "ticket": ticket,
                "closes": closes and ticket is not None,
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
        drop_ticket_links: bool,
    ) -> PullRequestUpdated:
        ref = store.locate(url, "pull_request")
        item = store.load(ref)
        if drop_ticket_links:
            item.update(ticket=None, closes=False)
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
        succeeds = self._checks == "success"
        conclusion = "success" if succeeds else "failure"
        result: dict[str, Any] = {
            "target": items.target(ref, item).model_dump(),
            "head_sha": head_sha,
            "current_head_sha": head_sha,
            "rollup": conclusion,
            "checks": [
                {
                    "name": _JOB,
                    "status": "completed",
                    "conclusion": conclusion,
                    "details_url": "",
                }
            ],
            "checks_expected": False,
        }
        if failed_logs:
            result["failed_logs"] = (
                []
                if succeeds
                else [
                    {
                        "job_id": f"{_JOB}@{_ATTEMPT}",
                        "name": _JOB,
                        "conclusion": "failure",
                        "url": "",
                        "artifact_names": store.artifacts(ref.place, head_sha),
                        "log": "",
                        "log_bytes": 0,
                        "tail_limit_bytes": log_tail_bytes,
                        "truncated": False,
                    }
                ]
            )
        if item["state"] != "open":
            return Readiness.model_validate(
                {
                    **result,
                    "base_sha": None,
                    "behind_by": None,
                    "out_of_date": None,
                    "current_base_sha": None,
                    "readiness": "not_applicable",
                    "completion_blockers": [],
                }
            )
        base_sha = store.branch_tip(ref.place, item["base"])
        if not base_sha:
            raise ServiceError(f"The base branch '{item['base']}' of {url} is gone.")
        behind_by = int(
            store.git(ref.place, "rev-list", "--count", f"{head_sha}..{base_sha}")
        )
        blockers = []
        if behind_by:
            blockers.append(
                {
                    "code": "base_out_of_date",
                    "message": f"The PR head is {behind_by} commit(s) behind its base.",
                    "next_action": "Merge or rebase the base branch, push, and check again.",
                }
            )
        if not succeeds:
            blockers.append(
                {
                    "code": "checks_failed",
                    "message": "CI checks are failing.",
                    "next_action": "Fix the failures and check again.",
                }
            )
        return Readiness.model_validate(
            {
                **result,
                "base_sha": base_sha,
                "behind_by": behind_by,
                "out_of_date": behind_by > 0,
                "current_base_sha": base_sha,
                "readiness": "blocked" if blockers else "ready",
                "completion_blockers": blockers,
            }
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
            if (task := self._pull_request_work(ref, pull)) is not None
        ]

    async def refresh_pull_request(self, task: Task) -> Task | None:
        assert task.pull_request_url
        ref = store.locate(task.pull_request_url, "pull_request")
        return self._pull_request_work(ref, store.load(ref))

    def _pull_request_work(self, ref: store.Ref, item: dict[str, Any]) -> Task | None:
        """Feedback the author has not answered, or a requested review of a
        head the member has not reviewed."""
        if item["state"] != "open" or item["draft"]:
            return None
        me = self.person.person_id
        said = sorted(
            (
                entry
                for key in ("comments", "reviews", "review_comments")
                for entry in item[key]
            ),
            key=lambda said: said["created_at"],
        )
        if item["author"] == me:
            work = _FEEDBACK if said and said[-1]["author"] != me else None
        else:
            reviewed = {r["commit"] for r in item["reviews"] if r["author"] == me}
            needed = me in item["reviewers"] and _head_sha(item) not in reviewed
            work = _REVIEW if needed else None
        if work is None:
            return None
        return items.task(
            ref,
            item,
            status=Task.IN_PROGRESS,
            assignee=me,
            pull_request_url=ref.url,
            trigger_reason=work,
        )


def _local_ticket(url: str) -> store.Ref | None:
    """The local ticket ``url`` names; ``None`` for another service's URL.

    Raises:
        ServiceNotFound: If it is a local URL that names no ticket.
    """
    if urlparse(url).scheme != store.SCHEME:
        return None
    return store.locate(url, "ticket")


def _linked(ticket_url: str, repository: str, closes: bool) -> str | None:
    """The ticket a new pull request of ``repository`` refers to, as it keeps
    it. A merge closes only a ticket of the repository's own.

    Raises:
        ServiceNotFound: If it is a local ticket that is not there, or a local
            URL of no ticket.
        ServiceUnsupported: If it is another repository's or another
            service's ticket, to be closed.
    """
    if not ticket_url:
        return None
    ticket = _local_ticket(ticket_url)
    if ticket is not None:
        store.load(ticket)
    if closes and (
        ticket is None or store.tracker(ticket.place).get("repository") != repository
    ):
        raise ServiceUnsupported(
            f"A merge into {repository} closes only a ticket of it, not {ticket_url}."
        )
    return ticket_url if ticket is None else ticket.url


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
