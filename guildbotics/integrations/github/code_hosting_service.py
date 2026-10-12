"""GitHub as a member's code host: pull requests, their reviews and CI, the
git remote, and the pull request patrol."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import IO, Any
from urllib.parse import urlparse

from guildbotics.entities import Task
from guildbotics.integrations.github.actions_client import GitHubActionsClient
from guildbotics.integrations.github.api import INLINE, GitHubItem, number_of
from guildbotics.integrations.github.github_utils import (
    get_github_username,
    get_person_github_token,
    normalize_login,
    parse_timestamp,
)
from guildbotics.integrations.github.issue_links import (
    append_issue_link,
    preserve_issue_links,
)
from guildbotics.integrations.github.pull_request_patrol import (
    MAX_REVIEW_ROUNDS,
    PULL_REQUEST_QUERY,
    REVIEW_LIMIT,
    REVIEW_LIMIT_REASON,
    PullRequest,
    parse_pull_request,
    pull_request_work,
)
from guildbotics.integrations.github.pull_requests import (
    GitHubPullRequests,
    head_of,
    head_sha_of,
)
from guildbotics.integrations.github.reads import (
    pull_request,
    read_alerts,
    read_collection,
    reading,
    request_of,
)
from guildbotics.integrations.github.repository_scope import (
    CONVERT_PULL_REQUEST_TO_DRAFT,
)
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
    ReadPage,
    ReviewVerdict,
    read_conditions,
)
from guildbotics.utils.i18n_tool import t

_LOGGER = logging.getLogger(__name__)
_VERDICTS: dict[ReviewVerdict, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
#: A repository's web address: owner and name.
_REPOSITORY_PATH_PARTS = 2


class GitHubCodeHostingService(GitHubPullRequests, CodeHostingService):
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
        request = request_of(self, resource, subject, conditions)
        with reading(resource):
            if resource == "dependency_alerts":
                repository = self.repository(subject)
                return await read_alerts(
                    self, repository, conditions, request, continuation
                )
            if resource == "ticket_pull_requests":
                if not self.serves(subject):
                    raise ServiceUnsupported(
                        f"GitHub knows the pull requests of its own issues, not of"
                        f" {subject}."
                    )
                return await read_collection(
                    self, resource, self.item(subject, "ticket"), request, continuation
                )
            item = self.item(subject, "pull_request")
            if resource == "pull_request_readiness":
                readiness = await self.readiness(subject, **conditions)
                return ReadPage.of(resource, [readiness], target=readiness.target)
            if resource == "pull_requests":
                data = await self.fetch(item, bounded=True)
                return ReadPage.of(
                    resource,
                    [pull_request(self, item, data)],
                    target=self.target(item, data),
                )
            return await read_collection(self, resource, item, request, continuation)

    # ------------------------------------------------------------------ #
    #   The member                                                       #
    # ------------------------------------------------------------------ #

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        if check:
            # /rate_limit is readable by every credential type (PAT, machine
            # user, and GitHub App installation token). /user would 403 for
            # installation tokens (github_apps members), so it cannot be used
            # as a generic credential probe.
            await self.get("/rate_limit")
        return {"github_username": get_github_username(self.person)}

    # ------------------------------------------------------------------ #
    #   Pull requests                                                    #
    # ------------------------------------------------------------------ #

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
        ticket_url: str,
        closes: bool,
    ) -> PullRequestCreated:
        repository = self.repository(repository)
        owner = repository.partition("/")[0]
        base_branch = base.strip() or await self.default_branch(repository)
        endpoint = f"/repos/{repository}/pulls"
        client = await self.client()
        existing = (
            await client.get(
                endpoint,
                params={
                    "head": f"{owner}:{head}",
                    "base": base_branch,
                    "state": "open",
                },
            )
        ).json()
        if existing:
            data, created = existing[0], False
        else:
            if ticket_url:
                body = await self._linked(repository, body, ticket_url, closes)
            data = (
                await client.post(
                    endpoint,
                    json={
                        "title": title,
                        "head": head,
                        "base": base_branch,
                        "body": body,
                        "draft": draft,
                    },
                )
            ).json()
            created = True
        item = self.item(str(data["html_url"]), "pull_request")
        return PullRequestCreated(
            created=created,
            draft=bool(data.get("draft", draft if created else False)),
            head=head,
            base=base_branch,
            target=self.target(item, data),
        )

    async def _linked(
        self, repository: str, body: str, ticket_url: str, closes: bool
    ) -> str:
        """``body`` with the link to the ticket ``ticket_url``.

        GitHub links, and closes on a merge, only an issue of the pull
        request's repository, by its number; another ticket is referred to by
        its URL.

        Raises:
            ServiceNotFound: If ``ticket_url`` is this GitHub's address of no
                issue that is there.
            ServiceUnsupported: If it is to close another ticket.
        """
        ticket = self.item(ticket_url, "ticket") if self.serves(ticket_url) else None
        if ticket is not None:
            await self.fetch(ticket)
            if ticket.repository.casefold() == repository.casefold():
                return append_issue_link(body, ticket.number, closes=closes)
        if closes:
            raise ServiceUnsupported(
                f"A merge into {repository} closes only an issue of it, not"
                f" {ticket_url}."
            )
        return f"{body.rstrip()}\n\nRefs {ticket_url}".lstrip()

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        drop_ticket_links: bool,
    ) -> PullRequestUpdated:
        item = self.item(url, "pull_request")
        payload: dict[str, Any] = {}
        if body is not None:
            if not drop_ticket_links:
                previous = await self.fetch(item)
                body = preserve_issue_links(body, previous.get("body") or "")
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        data = (await (await self.client()).patch(item.api, json=payload)).json()
        return PullRequestUpdated(
            body=data.get("body") or "", target=self.target(item, data)
        )

    async def review(
        self, url: str, body: str, verdict: ReviewVerdict, head: str
    ) -> ReviewSubmitted:
        """Submit a review verdict on the PR head as a GitHub review.

        A review, unlike a conversation comment, is what GitHub counts: it
        consumes a pending review request and lists the member under
        ``reviewed-by``, so the patrol can follow the PR from then on.
        """
        item = self.item(url, "pull_request")
        data = await self._at_head(item, head)
        review = (
            await (await self.client()).post(
                f"{item.api}/reviews",
                json={
                    "body": body.rstrip(),
                    "event": _VERDICTS[verdict],
                    "commit_id": head,
                },
            )
        ).json()
        return ReviewSubmitted(
            verdict=verdict,
            commit=head,
            url=review.get("html_url"),
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
        data = await self._at_head(item, head)
        payload: dict[str, Any] = {
            "body": body.rstrip(),
            "commit_id": head,
            "path": path,
            "line": line,
            "side": side,
        }
        if start_line is not None and start_side is not None:
            payload["start_line"] = start_line
            payload["start_side"] = start_side
        comment = (
            await (await self.client()).post(f"{item.api}/comments", json=payload)
        ).json()
        return ReviewCommentWritten(
            id=f"{INLINE}{comment['id']}",
            url=comment.get("html_url"),
            created_at=comment.get("created_at"),
            path=path,
            line=line,
            side=side,
            target=self.target(item, data),
        )

    async def _at_head(self, item: GitHubItem, head: str) -> dict[str, Any]:
        """The pull request, if its head is still ``head``.

        Raises:
            ServiceError: If it is not.
        """
        data = await self.fetch(item)
        current = head_sha_of(item, data)
        if current != head:
            raise ServiceError(
                f"The head of {self.url(item)} is now {current}, not {head}: read"
                " the new commits, then submit again on that head."
            )
        return data

    async def reply(self, url: str, reply_to: str, body: str) -> ReplyWritten:
        item = self.item(url, "pull_request")
        data = await self.fetch(item)
        number = number_of(reply_to, INLINE)
        root = await self.get(f"/repos/{item.repository}/pulls/comments/{number}")
        if (
            not isinstance(root, dict)
            or str(root.get("pull_request_url")).casefold()
            != f"{self.base_url}{item.api}".casefold()
            or root.get("in_reply_to_id") is not None
        ):
            raise ServiceNotFound(
                f"'{reply_to}' roots no review thread of {self.url(item)}."
            )
        reply = (
            await (await self.client()).post(
                f"{item.api}/comments/{number}/replies", json={"body": body.rstrip()}
            )
        ).json()
        return ReplyWritten(
            id=f"{INLINE}{reply['id']}",
            url=reply.get("html_url"),
            created_at=reply.get("created_at"),
            target=self.target(item, data),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        return await self.react(self.item(url, "pull_request"), target_id, reaction)

    async def open_pull_requests(self, repository: str, branch: str) -> list[str]:
        repository = self.repository(repository)
        # Member publishing targets branches in the configured origin repository;
        # fork-owned heads are outside this interactive push lookup.
        found = await self.get(
            f"/repos/{repository}/pulls",
            params={
                "state": "open",
                "head": f"{repository.partition('/')[0]}:{branch}",
            },
        )
        return [str(pull["html_url"]) for pull in found]

    @asynccontextmanager
    async def artifact(
        self, url: str, name: str, limit: int
    ) -> AsyncIterator[tuple[ArtifactRef, IO[bytes]]]:
        item = self.item(url, "pull_request")
        head_sha = head_sha_of(item, await self.fetch(item))
        actions = GitHubActionsClient(await self.client())
        artifacts = await actions.artifacts(item.repository, name=name)
        candidates = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == name
            and not artifact.get("expired", False)
            and (artifact.get("workflow_run") or {}).get("head_sha") == head_sha
        ]
        if not candidates:
            raise ServiceNotFound(
                f"The head of {url} has no artifact '{name}'.",
            )
        artifact = max(candidates, key=lambda candidate: int(candidate.get("id", 0)))
        artifact_id = int(artifact["id"])
        archive_size = int(artifact.get("size_in_bytes", 0))
        if archive_size > limit:
            raise ServiceError(
                f"Artifact '{name}' is {archive_size} bytes, above the "
                f"{limit} byte limit. Ask a human to retrieve it."
            )
        async with actions.artifact_archive(
            item.repository, artifact_id, limit
        ) as archive:
            yield ArtifactRef(name=name, id=str(artifact_id)), archive

    # ------------------------------------------------------------------ #
    #   The git remote                                                   #
    # ------------------------------------------------------------------ #

    async def clone_url(self, repository: str) -> str:
        return f"{self.web_base_url()}/{self.repository(repository)}.git"

    async def default_branch(self, repository: str) -> str:
        data = await self.get(f"/repos/{self.repository(repository)}")
        return str(data.get("default_branch") or "main")

    async def pull_request_head(self, url: str) -> PullRequestHead:
        item = self.item(url, "pull_request")
        return head_of(item, await self.fetch(item))

    async def push_credential(self) -> str:
        return await get_person_github_token(self.person, self.base_url)

    def check_push(self, repository: str) -> None:
        check_path(self.owner, repository)

    def commit_url(self, repository: str, sha: str) -> str:
        return f"{self.web_base_url()}/{repository}/commit/{sha}"

    def repository_from_remote(self, remote_url: str) -> str | None:
        web_url = _remote_web_url(remote_url)
        parsed = urlparse(web_url)
        if not web_url or parsed.hostname != urlparse(self.web_base_url()).hostname:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != _REPOSITORY_PATH_PARTS:
            return None
        return "/".join(parts)

    # ------------------------------------------------------------------ #
    #   Pull request patrol                                              #
    # ------------------------------------------------------------------ #

    async def pull_request_candidates(self) -> list[Task]:
        """Every open PR that asks something of the member, oldest update first."""
        candidates: list[Task] = []
        for found in await self._search_pull_requests():
            task = await self._pull_request_work(str(found["html_url"]))
            if task is not None:
                candidates.append(task)
        return candidates

    async def refresh_pull_request(self, task: Task) -> Task | None:
        assert task.pull_request_url
        return await self._pull_request_work(task.pull_request_url)

    async def _pull_request_work(self, url: str) -> Task | None:
        """What the PR asks of the member now; one at the re-review limit is
        handed over instead."""
        item = self.item(url, "pull_request")
        owner, name = item.repository.split("/")
        data = await self.graphql(
            PULL_REQUEST_QUERY, {"owner": owner, "repo": name, "number": item.number}
        )
        node = (data.get("repository") or {}).get("pullRequest")
        if not node:
            raise ServiceNotFound(f"{url} is not there.")
        pull = parse_pull_request(node, item.repository)
        work = pull_request_work(pull, self._login())
        if work is None:
            return None
        task = self._pull_request_task(pull, work)
        if work == REVIEW_LIMIT:
            await self._hand_over_at_review_limit(task)
            return None
        return task

    async def _search_pull_requests(self) -> list[dict[str, Any]]:
        """Open PRs under the project owner that name this member.

        Search is the one listing that needs no repository list and works for
        both PAT and GitHub App logins (``<app>[bot]``). Three qualifiers cover
        the two roles: written by, reviewed by, and review requested from the
        member (the last never matches a GitHub App, which GitHub cannot
        request a review from).

        Draft PRs are left out at the search: a draft is the human's "hands
        off" switch (the patrol flips it only to hand a PR over at the review
        limit), so neither role acts on it until someone marks the PR ready for
        review.
        """
        username = get_github_username(self.person, strict=True)
        found: dict[str, dict[str, Any]] = {}
        for qualifier in ("author", "reviewed-by", "review-requested"):
            page = await self.get(
                "/search/issues",
                params={
                    "q": (
                        f"is:pr is:open draft:false user:{self.owner} "
                        f"{qualifier}:{username}"
                    ),
                    "per_page": 100,
                    "sort": "updated",
                    "order": "asc",
                },
            )
            for pull in page.get("items") or []:
                found.setdefault(str(pull.get("html_url") or ""), pull)
        return sorted(
            found.values(), key=lambda pull: str(pull.get("updated_at") or "")
        )

    def _login(self) -> str:
        return normalize_login(get_github_username(self.person, strict=True))

    def _pull_request_task(self, pull: PullRequest, trigger_reason: str) -> Task:
        return Task(
            id=pull.node_id,
            url=pull.url,
            title=pull.title,
            description=pull.body,
            status=Task.IN_PROGRESS,
            created_at=parse_timestamp(pull.created_at),
            assignee=self.person.person_id,
            pull_request_url=pull.url,
            trigger_reason=trigger_reason,
        )

    async def _hand_over_at_review_limit(self, task: Task) -> None:
        """Make the PR a draft, then say on it that automatic re-review stopped.

        The notice tells the human that the PR is theirs until they mark it
        ready for review, so it is posted only once the PR is a draft. When
        the conversion fails, a failure notice takes its place: it holds the
        PR until someone acts on it, and the rounds stay as they are.
        """
        assert task.pull_request_url
        try:
            await self.graphql(CONVERT_PULL_REQUEST_TO_DRAFT, {"pullRequest": task.id})
        except Exception as exc:
            _LOGGER.warning(
                f"Could not convert {task.pull_request_url} to a draft: {exc}"
            )
            status = WorkflowStatus(
                reason="failed",
                person_id=self.person.person_id,
                run_id="",
                subject=task.pull_request_url,
            )
            body = t(
                "integrations.github.code_hosting_service.review_limit_draft_failed",
                count=MAX_REVIEW_ROUNDS,
            )
        else:
            status = WorkflowStatus(
                reason=REVIEW_LIMIT_REASON,
                person_id=self.person.person_id,
                run_id="",
                subject=task.pull_request_url,
            )
            body = t(
                "integrations.github.code_hosting_service.review_limit_reached",
                count=MAX_REVIEW_ROUNDS,
            )
        await self.comment(task.pull_request_url, body, status=status)


def _remote_web_url(remote_url: str) -> str:
    value = remote_url.strip()
    if not value:
        return ""
    if "://" not in value and ":" in value:
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        return _strip_git_suffix(f"https://{host}/{path}") if host and path else ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path:
        return _strip_git_suffix(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return _strip_git_suffix(f"https://{parsed.hostname}{parsed.path}")
    return ""


def _strip_git_suffix(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")
