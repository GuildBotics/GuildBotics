"""Branvil as a member's code host: pull requests, their reviews and CI, the
git remote, and the pull request patrol.

Branvil keeps what is said on a pull request as it was sent -- a workflow
status as the comment's ``meta`` -- and judges nothing of GuildBotics' own:
the patrol's rules (:mod:`~guildbotics.integrations.pull_request_patrol`)
and readiness (:mod:`~guildbotics.integrations.readiness`) are applied here
to what it answers. A pull request that links an interactive ticket is
reviewed as any other, but its author's part is left to the human working on
that ticket with an agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import IO, Any

from guildbotics.entities import Person, Service, Task, Team
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    BranvilApi,
    BranvilItem,
    interactive,
    segment,
    username,
)
from guildbotics.integrations.diff_lines import file_change
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
from guildbotics.integrations.readiness import (
    FAILED_CONCLUSIONS,
    Freshness,
    effective_tail_bytes,
    inspected,
    readiness_of,
)
from guildbotics.integrations.repository_scope import check_path, in_scope, valid_path
from guildbotics.runtime.code_hosting_service import (
    ArtifactRef,
    CodeHostingService,
    GitCredential,
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


class BranvilCodeHostingService(BranvilApi, CodeHostingService):
    def __init__(self, person: Person, team: Team) -> None:
        super().__init__(person, team, Service.CODE_HOSTING_SERVICE)

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
            alerts = (
                f"/repos/{segment(self.repository(subject))}/upstream/dependency-alerts"
            )
            if conditions["id"]:
                alert = await self.get(f"{alerts}/{segment(conditions['id'])}")
                return ReadPage.of(resource, [alert])
            return await self.page(
                resource,
                alerts,
                conditions,
                continuation,
                dict,
                state=conditions["state"],
            )
        if resource == "ticket_pull_requests":
            if not self.serves(subject):
                raise ServiceUnsupported(
                    t("integrations.branvil.api.foreign_ticket", url=subject)
                )
            ticket = self.item(subject, "ticket")
            return await self.page(
                resource, f"{ticket.api}/pulls", conditions, continuation, _referring
            )
        item = self.item(subject, "pull_request")
        if resource == "pull_requests":
            data = await self.get(item.api)
            return ReadPage.of(
                resource,
                [self._pull_request(item, data)],
                target=self.target(item, data),
            )
        if resource == "pull_request_readiness":
            readiness = await self.readiness(subject, **conditions)
            return ReadPage.of(resource, [readiness], target=readiness.target)
        collection, translate = {
            "pull_request_comments": ("comments", self.comment_of),
            "pull_request_reviews": ("reviews", self._review),
            "pull_request_files": ("files", _file),
            "pull_request_threads": ("threads", self._thread),
            "review_thread_comments": (
                f"threads/{segment(conditions['node'])}/comments",
                self._thread_comment,
            ),
        }[resource]
        return await self.page(
            resource, f"{item.api}/{collection}", conditions, continuation, translate
        )

    def _pull_request(self, item: BranvilItem, data: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.target(item, data).model_dump(),
            "repository": str(data["repository"]),
            "body": str(data.get("body") or ""),
            "state": str(data["state"]),
            "assignees": [],
            "labels": list(data.get("labels") or []),
            "author_type": self.author_type(data.get("author")),
            "head": str(data["head"]),
            "head_repository": str(data["head_repository"]),
            "head_sha": str(data.get("head_sha") or ""),
            "base": str(data["base"]),
            "merged": bool(data.get("merged")),
            "draft": bool(data.get("draft")),
            "changed_files": data.get("changed_files"),
        }

    def _review(self, review: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(review["id"]),
            "body": str(review.get("body") or ""),
            "author": str(review.get("author") or ""),
            "author_type": self.author_type(review.get("author")),
            "verdict": review["verdict"],
            "commit": review.get("commit"),
            "submitted_at": review.get("submitted_at"),
            "url": review.get("url"),
        }

    def _thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(thread["id"]),
            "resolved": thread.get("resolved"),
            "outdated": thread.get("outdated"),
            "comments": [self._thread_comment(c) for c in thread["comments"]],
            "comments_complete": True,
        }

    def _thread_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        return {**self.comment_of(comment), "reply_to_id": comment.get("reply_to_id")}

    # -- the member ---------------------------------------------------------

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        if check:
            await self.confirm()
        return {"branvil_username": username(self.person)}

    # -- pull requests ------------------------------------------------------

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        return await self.post_comment(self.item(url, "pull_request"), body, status)

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
        repository = self.repository(repository)
        links = await self._ticket_links(repository, tickets)
        answer = await self.call(
            "POST",
            f"/repos/{segment(repository)}/pulls",
            {
                "head": head,
                "base": base.strip(),
                "title": title,
                "body": body,
                "draft": draft,
                "tickets": links,
            },
        )
        pull = answer["pull"]
        return PullRequestCreated(
            created=bool(answer["created"]),
            draft=bool(pull.get("draft")),
            head=head,
            base=str(pull["base"]),
            target=self.target(_pull_item(pull), pull),
        )

    async def _ticket_links(
        self, repository: str, tickets: Sequence[TicketLink]
    ) -> list[dict[str, Any]]:
        """Validate every link before creating or updating a pull request."""
        links = []
        for ticket in tickets:
            url = ticket.url
            item = self.item(url, "ticket") if self.serves(url) else None
            linked = await self.get(item.api) if item is not None else None
            if ticket.closes and (
                linked is None
                or str(linked["repository"]).casefold() != repository.casefold()
            ):
                raise ServiceUnsupported(
                    t(
                        "integrations.branvil.api.closes_foreign",
                        repository=repository,
                        url=url,
                    )
                )
            if item is not None:
                url = self.url(item)
            links.append({"url": url, "closes": ticket.closes})
        return links

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        tickets: Sequence[TicketLink] | None,
    ) -> PullRequestUpdated:
        item = self.item(url, "pull_request")
        changes: dict[str, Any] = {"body": body, "title": title}
        if tickets is not None:
            changes["tickets"] = await self._ticket_links(item.place, tickets)
        data = await self.call(
            "PATCH",
            item.api,
            {key: value for key, value in changes.items() if value is not None},
        )
        return PullRequestUpdated(
            body=str(data.get("body") or ""), target=self.target(item, data)
        )

    async def review(
        self, url: str, body: str, verdict: ReviewVerdict, head: str
    ) -> ReviewSubmitted:
        item = self.item(url, "pull_request")
        data = await self.get(item.api)
        review = await self.call(
            "POST",
            f"{item.api}/reviews",
            {"verdict": verdict, "body": body.rstrip(), "commit": head},
        )
        return ReviewSubmitted(
            verdict=verdict,
            commit=head,
            url=review.get("url"),
            target=self.target(item, data),
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
        item = self.item(url, "pull_request")
        data = await self.get(item.api)
        payload: dict[str, Any] = {
            "body": body.rstrip(),
            "commit": head,
            "path": path,
            "line": line,
            "side": side,
        }
        if start_line is not None and start_side is not None:
            payload.update(start_line=start_line, start_side=start_side)
        written = await self.call("POST", f"{item.api}/inline-comments", payload)
        return ReviewCommentWritten(
            id=str(written["id"]),
            url=written.get("url"),
            created_at=written.get("created_at"),
            path=path,
            line=line,
            side=side,
            target=self.target(item, data),
        )

    async def reply(self, url: str, reply_to: str, body: str) -> ReplyWritten:
        item = self.item(url, "pull_request")
        data = await self.get(item.api)
        written = await self.call(
            "POST",
            f"{item.api}/threads/{segment(reply_to)}/replies",
            {"body": body.rstrip()},
        )
        return ReplyWritten(
            id=str(written["id"]),
            url=written.get("url"),
            created_at=written.get("created_at"),
            target=self.target(item, data),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        return await self.react(self.item(url, "pull_request"), target_id, reaction)

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        """The head's checks, as Branvil read them with the head and the base,
        judged against the pull request read again after them."""
        ReadinessQuery(failed_logs=failed_logs, log_tail_bytes=log_tail_bytes)
        item = self.item(url, "pull_request")
        report = await self.get(f"{item.api}/checks")
        logs = (
            await self._failed_logs(item, report["checks"], log_tail_bytes)
            if failed_logs
            else None
        )
        current = await self.get(item.api)
        freshness = None
        if current["state"] == "open":
            if not report["head_sha"]:
                raise ServiceError(t("integrations.branvil.api.no_head", url=url))
            if not (report["base_sha"] and current["base_sha"]):
                raise ServiceError(t("integrations.branvil.api.no_base", url=url))
            freshness = Freshness(
                str(report["base_sha"]),
                int(report["behind_by"]),
                str(current["base_sha"]),
                requires_up_to_date=False,
            )
        return readiness_of(
            target=self.target(item, current),
            head_sha=str(report["head_sha"]),
            current_head_sha=str(current["head_sha"]),
            checks=[
                {
                    "name": str(check["name"]),
                    "status": str(check["status"]),
                    "conclusion": check.get("conclusion"),
                    "details_url": str(check.get("details_url") or ""),
                    "details": str(check.get("details") or ""),
                }
                for check in report["checks"]
            ],
            checks_expected=bool(report.get("checks_expected")),
            freshness=freshness,
            failed_logs=logs,
        )

    async def _failed_logs(
        self, item: BranvilItem, checks: list[dict[str, Any]], log_tail_bytes: int
    ) -> list[dict[str, Any]]:
        failed = [
            check
            for check in checks
            if check.get("conclusion") in FAILED_CONCLUSIONS and check.get("attempt_id")
        ]
        selected = inspected(failed, lambda check: check["conclusion"])
        if not selected:
            return []
        tail_bytes = effective_tail_bytes(log_tail_bytes, len(selected))
        logs = []
        for check in selected:
            attempt = str(check["attempt_id"])
            found = await self.get(
                f"/repos/{segment(item.place)}/jobs/{segment(attempt)}/log",
                tail_bytes=tail_bytes,
            )
            log = str(found["log"])
            logs.append(
                {
                    "job_id": attempt,
                    "name": str(check["name"]),
                    "conclusion": str(check["conclusion"]),
                    "url": str(check.get("details_url") or ""),
                    "artifact_names": list(check.get("artifact_names") or []),
                    "log": log,
                    "log_bytes": len(log.encode()),
                    "tail_limit_bytes": tail_bytes,
                    "truncated": bool(found.get("truncated")),
                }
            )
        return logs

    async def open_pull_requests(self, repository: str, branch: str) -> list[str]:
        pulls = await self.items(
            f"/repos/{segment(self.repository(repository))}/pulls",
            state="open",
            head=branch,
        )
        return [self.url(_pull_item(pull)) for pull in pulls]

    @asynccontextmanager
    async def artifact(
        self, url: str, name: str, limit: int
    ) -> AsyncIterator[tuple[ArtifactRef, IO[bytes]]]:
        item = self.item(url, "pull_request")
        head = str((await self.get(item.api)).get("head_sha") or "")
        path = f"/repos/{segment(item.place)}/commits/{head}/artifacts/{segment(name)}"
        too_large = ServiceError(
            t("integrations.branvil.api.artifact_too_large", name=name, limit=limit)
        )
        async with self.download(path, limit, too_large) as archive:
            yield ArtifactRef(name=name, id=f"{head}/{name}"), archive

    # -- the git remote -----------------------------------------------------

    async def clone_url(self, repository: str) -> str:
        return f"{self.base_url}/git/{self.repository(repository)}.git"

    async def default_branch(self, repository: str) -> str:
        data = await self.get(f"/repos/{segment(self.repository(repository))}")
        return str(data["default_branch"])

    async def pull_request_head(self, url: str) -> PullRequestHead:
        data = await self.get(self.item(url, "pull_request").api)
        return PullRequestHead(
            repository=str(data["head_repository"]), branch=str(data["head"])
        )

    async def git_credential(self) -> GitCredential:
        """The member's name and token: Branvil's Smart HTTP takes a member's
        token as the password of the member's name.

        It is handed out once Branvil answers, which the member's client
        waits for while Branvil restarts, so that git does not meet a Branvil
        that is restarting. Without a token, nothing is asked of Branvil.
        """
        name = username(self.person)
        if not name:
            raise ServiceError(t("integrations.branvil.api.username_missing"))
        await self.get("/health")
        return GitCredential(
            username=name, password=self.person.get_secret("BRANVIL_TOKEN")
        )

    def check_push(self, repository: str) -> None:
        check_path(self.workspace, repository, provider=PROVIDER)

    def commit_url(self, repository: str, sha: str) -> str:
        return f"{self.base_url}/{repository}/-/commits/{sha}"

    def repository_from_remote(self, remote_url: str) -> str | None:
        prefix = f"{self.base_url}/git/"
        remote = remote_url.strip()
        repository = remote.removeprefix(prefix).removesuffix(".git")
        return (
            repository if remote.startswith(prefix) and valid_path(repository) else None
        )

    # -- the pull request patrol --------------------------------------------

    async def pull_request_candidates(self) -> list[Task]:
        """Every open, ready PR in the scope that involves the member, oldest
        update first, that asks something of it."""
        entries = await self.items("/pulls", involves="me", state="open", draft="false")
        return [
            task
            for entry in entries
            if in_scope(self.workspace, str(entry["repository"]))
            and (task := await self._work(entry, entry["activity"])) is not None
        ]

    async def refresh_pull_request(self, task: Task) -> Task | None:
        assert task.pull_request_url
        item = self.item(task.pull_request_url, "pull_request")
        pull = await self.get(item.api)
        return await self._work(pull, await self.get(f"{item.api}/activity"))

    async def can_receive_work(self, url: str) -> bool:
        item = self.item(url, "pull_request")
        pull = await self.get(item.api)
        if not pull.get("head_sha"):
            return False
        me = username(self.person).casefold()
        if _name(pull) == me and await self._interactive(pull):
            return False
        activity = await self.get(f"{item.api}/activity")
        return pull_request_work(self._snapshot(item, pull, activity), me) in {
            FEEDBACK,
            REVIEW,
        }

    async def _work(
        self, pull: dict[str, Any], activity: dict[str, Any]
    ) -> Task | None:
        if not pull.get("head_sha"):
            return None  # the head branch is gone: there is nothing to work on
        item = _pull_item(pull)
        me = username(self.person).casefold()
        task = await patrol(
            self,
            self._snapshot(item, pull, activity),
            me,
            provider=PROVIDER,
            person_id=self.person.person_id,
            draft=lambda: self.call("PATCH", item.api, {"draft": True}),
        )
        if task is not None and _name(pull) == me and await self._interactive(pull):
            return None
        return task

    async def _interactive(self, pull: dict[str, Any]) -> bool:
        """Whether ``pull`` links an interactive ticket: a human works on it
        with an agent, so the patrol leaves the author's work on the pull
        request -- its feedback and its checks -- to them. A link that is
        not to this Branvil names no ticket of it."""
        for link in pull["tickets"]:
            if self.serves(link["url"]) and interactive(
                await self.get(self.item(link["url"], "ticket").api)
            ):
                return True
        return False

    def _snapshot(
        self, item: BranvilItem, pull: dict[str, Any], activity: dict[str, Any]
    ) -> PullRequest:
        """The pull request as the patrol sees it; names are case-folded, as
        Branvil compares them."""
        verdicts = [
            Review(
                author=_name(review),
                body=str(review.get("body") or ""),
                submitted_at=str(review.get("submitted_at") or ""),
                commit_oid=str(review.get("commit") or ""),
                reply_only=bool(review.get("reply_only")),
                reactors=_reactors(review),
            )
            for review in activity["reviews"]
        ]
        # An inline comment is a review of the commit it is on, as on GitHub:
        # a root one stands alone, a reply only answers.
        inline = [
            Review(
                author=_name(comment),
                body="",
                submitted_at=str(comment.get("created_at") or ""),
                commit_oid=str(thread.get("commit") or ""),
                reply_only=comment.get("reply_to_id") is not None,
                reactors=frozenset(),
            )
            for thread in activity["threads"]
            for comment in thread["comments"]
        ]
        reviews = sorted(verdicts + inline, key=lambda review: review.submitted_at)
        comments = sorted(
            (
                Comment(
                    author=_name(comment),
                    created_at=str(comment.get("created_at") or ""),
                    reactors=_reactors(comment),
                    status=WorkflowStatus.from_payload(comment.get("meta")),
                )
                for comment in activity["comments"]
            ),
            key=lambda comment: comment.created_at,
        )
        threads = [
            ReviewThread(
                resolved=bool(thread.get("resolved")),
                participants=frozenset(_name(c) for c in thread["comments"]),
                last_author=_name(thread["comments"][-1]),
                last_created_at=str(thread["comments"][-1].get("created_at") or ""),
                last_reactors=_reactors(thread["comments"][-1]),
            )
            for thread in activity["threads"]
            if thread["comments"]
        ]
        url = self.url(item)
        return PullRequest(
            id=url,
            label=item.label,
            url=url,
            title=str(pull.get("title") or ""),
            body=str(pull.get("body") or ""),
            open=pull["state"] == "open",
            is_draft=bool(pull.get("draft")),
            created_at=str(pull.get("created_at") or ""),
            author=_name(pull),
            head_oid=str(pull.get("head_sha") or ""),
            ready_at=str(pull.get("ready_at") or ""),
            # The pending review requests: a review consumes its author's.
            requested_reviewers=frozenset(
                str(name).casefold() for name in pull.get("reviewers") or []
            ),
            reviews=tuple(reviews),
            comments=tuple(comments),
            threads=tuple(threads),
        )


def _pull_item(pull: dict[str, Any]) -> BranvilItem:
    return BranvilItem("pull_request", str(pull["repository"]), str(pull["number"]))


def _name(entry: dict[str, Any]) -> str:
    return str(entry.get("author") or "").casefold()


def _reactors(entry: dict[str, Any]) -> frozenset[str]:
    return frozenset(_name(reaction) for reaction in entry.get("reactions") or [])


def _referring(pull: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(pull["url"]),
        "label": str(pull["label"]),
        "title": str(pull.get("title") or ""),
        "state": str(pull["state"]),
        "merged": bool(pull.get("merged")),
    }


def _file(file: dict[str, Any]) -> dict[str, Any]:
    additions, deletions = int(file["additions"]), int(file["deletions"])
    return file_change(
        str(file["path"]),
        str(file["status"]),
        file.get("patch"),
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
    )
