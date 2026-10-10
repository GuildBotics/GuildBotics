"""The local code host: repositories whose issues and pull requests are files
of the workspace (:mod:`.store`), and whose remote is a bare repository.

Whether a pull request's CI passes is the project's setting
``services.code_hosting_service.checks`` (``success``, the default, or
``failure``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.local import store
from guildbotics.integrations.repository_scope import (
    check_repository,
    configured_owner,
)
from guildbotics.runtime.code_hosting_resources import (
    DETAIL_RESOURCES,
    CompletionBlocker,
    Readiness,
    ReadinessQuery,
    RepositoryReadError,
    RepositoryReadPage,
    WorkTarget,
    read_conditions,
)
from guildbotics.runtime.code_hosting_service import (
    ArtifactRef,
    CodeHostingService,
    CommentWritten,
    IssueCreated,
    IssueUpdated,
    ItemKind,
    ItemRef,
    PullRequestCreated,
    PullRequestHead,
    PullRequestUpdated,
    ReactionAdded,
    ReactionTarget,
    ReplyWritten,
    ReviewCommentWritten,
    ReviewEvent,
    ReviewSubmitted,
)
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.utils.i18n_tool import t

_REVIEW_STATES = {
    "approve": "APPROVED",
    "request-changes": "CHANGES_REQUESTED",
    "comment": "COMMENTED",
}
_FEEDBACK = "pull_request_feedback"
_REVIEW = "pull_request_review"


class LocalCodeHostingService(CodeHostingService):
    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.team = team

    async def aclose(self) -> None:
        """The files are opened per operation."""

    # -- reads --------------------------------------------------------------

    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        conditions = read_conditions(resource, repo, identifier, parameters)
        owner, name = store.split(repo)
        if resource == "dependency_alerts":
            return RepositoryReadPage.of(resource, [])
        if continuation and (
            resource in DETAIL_RESOURCES or not continuation.isdigit()
        ):
            raise RepositoryReadError(t("integrations.repository.continuation"))
        try:
            item = store.load(owner, name, int(identifier))
        except MemberCapabilityError as exc:
            raise RepositoryReadError(str(exc)) from None
        target = _target(owner, name, item)
        if resource == "pull_request_readiness":
            result = await self.readiness(target.html_url, **conditions)
            return RepositoryReadPage.of(resource, [result], target=target)
        if resource in DETAIL_RESOURCES:
            return RepositoryReadPage.of(
                resource, [self._detail(owner, name, item)], target=target
            )
        entries = self._entries(resource, owner, name, item, conditions)
        start = int(continuation or 0)
        end = start + conditions["page_size"]
        return RepositoryReadPage.of(
            resource,
            entries[start:end],
            continuation=str(end) if end < len(entries) else None,
        )

    def _detail(self, owner: str, repo: str, item: dict[str, Any]) -> dict[str, Any]:
        detail = {
            **_target(owner, repo, item).model_dump(),
            "body": item["body"],
            "state": item["state"],
            "assignees": item.get("assignees", []),
            "labels": item.get("labels", []),
        }
        if item["kind"] == "pull_request":
            head_owner, head_repo = store.split(
                item.get("head_repo") or f"{owner}/{repo}"
            )
            detail.update(
                author_type=self._author_type(item["author"]),
                head=item["head"],
                head_repo=f"{head_owner}/{head_repo}",
                head_owner=head_owner,
                head_repo_name=head_repo,
                head_sha=_head_sha(owner, repo, item),
                base=item["base"],
                merged=item.get("merged", False),
                draft=item.get("draft", False),
                changed_files=None,
            )
        return detail

    def _entries(
        self,
        resource: str,
        owner: str,
        repo: str,
        item: dict[str, Any],
        conditions: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if resource == "issue_comments":
            return [self._comment(comment) for comment in item.get("comments", [])]
        if resource == "issue_timeline":
            return [
                {
                    "event": "cross-referenced",
                    "pull_request": {"repo": f"{owner}/{repo}", "number": pr["number"]},
                }
                for pr in store.items(owner, repo)
                if pr["kind"] == "pull_request" and pr.get("issue") == item["number"]
            ]
        if resource == "issue_projects":
            if not item.get("lane"):
                return []
            return [
                {
                    "item_id": _target(owner, repo, item).html_url,
                    "project_title": "local",
                    "project_number": None,
                    "project_url": None,
                    "fields": [{"field": "Status", "value": item["lane"]}],
                }
            ]
        if resource == "pull_request_reviews":
            return [
                {
                    "id": review["id"],
                    "body": review["body"],
                    "author": review["author"],
                    "author_type": self._author_type(review["author"]),
                    "state": review["state"],
                    "submitted_at": review["submitted_at"],
                    "html_url": None,
                    "commit_id": review["commit_id"],
                }
                for review in item.get("reviews", [])
            ]
        if resource == "pull_request_files":
            return []
        threads = _threads(item)
        if resource == "pull_request_threads":
            return [
                {
                    "id": str(root["id"]),
                    "resolved": False,
                    "outdated": False,
                    "comments": [self._thread_comment(c) for c in comments],
                    "comments_complete": True,
                }
                for root, comments in threads
            ]
        return [
            self._thread_comment(comment)
            for root, comments in threads
            if str(root["id"]) == conditions["node"]
            for comment in comments
        ]

    def _comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": comment["id"],
            "body": comment["body"],
            "author": comment["author"],
            "author_type": self._author_type(comment["author"]),
            "created_at": comment["created_at"],
            "html_url": None,
        }

    def _thread_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        return {**self._comment(comment), "reply_to_id": comment.get("in_reply_to")}

    def _author_type(self, author: str) -> str:
        return Message.ASSISTANT if author == self.person.person_id else Message.USER

    def locate(self, url: str, kind: ItemKind | None = None) -> ItemRef:
        return store.locate(url, kind)

    async def identity(self, *, check: bool = False) -> dict[str, str]:
        return {}

    # -- issues -------------------------------------------------------------

    async def comment(
        self,
        url: str,
        body: str,
        *,
        kind: ItemKind | None = None,
        status: dict[str, object] | None = None,
    ) -> CommentWritten:
        ref, item = self._writable(url, kind)
        comment = self._entry(ref, {"body": body.rstrip(), "status": status})
        item.setdefault("comments", []).append(comment)
        store.save(ref.owner, ref.repo, item)
        return CommentWritten(
            comment_id=comment["id"],
            html_url=f"{ref.url}#comment-{comment['id']}",
            author=self.person.person_id,
            created_at=comment["created_at"],
            target=_target(ref.owner, ref.repo, item),
        )

    async def create_issue(
        self, repo: str, title: str, body: str, labels: Sequence[str]
    ) -> IssueCreated:
        owner, name = self._writable_repository(repo)
        item = self._new_item(owner, name, "issue", title, body)
        item["labels"] = self._defined_labels(owner, name, labels)
        store.save(owner, name, item)
        target = _target(owner, name, item)
        return IssueCreated(
            issue_number=item["number"],
            issue_title=title,
            repo=repo,
            issue_url=target.html_url,
            labels=item["labels"],
            target=target,
        )

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
        ref, item = self._writable(url, "issue")
        additions = self._defined_labels(ref.owner, ref.repo, add_labels)
        state_changed = state is not None and item["state"] != state
        for key, value in (("body", body), ("title", title), ("state", state)):
            if value is not None:
                item[key] = value
        if state_changed:
            item["closed_at"] = store.now() if state == "closed" else None
        labels = [label for label in item["labels"] if label not in remove_labels]
        item["labels"] = labels + [label for label in additions if label not in labels]
        store.save(ref.owner, ref.repo, item)
        return IssueUpdated(
            issue_number=ref.number,
            issue_url=ref.url,
            repo=ref.full_repo,
            title=item["title"],
            state=item["state"],
            state_changed=state_changed,
            labels=item["labels"],
            body=item["body"],
            target=_target(ref.owner, ref.repo, item),
        )

    async def linked_pull_requests(self, issue_url: str) -> list[str]:
        ref = store.locate(issue_url, "issue")
        return [
            store.url(ref.owner, ref.repo, "pull_request", item["number"])
            for item in store.items(ref.owner, ref.repo)
            if item["kind"] == "pull_request" and item.get("issue") == ref.number
        ]

    # -- pull requests ------------------------------------------------------

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
        owner, name = self._writable_repository(repo)
        base = base.strip() or await self.default_branch(owner, name)
        item = next(
            (
                pr
                for pr in store.items(owner, name)
                if pr["kind"] == "pull_request"
                and pr["state"] == "open"
                and (pr["head"], pr["base"]) == (head, base)
            ),
            None,
        )
        created = item is None
        if item is None:
            item = self._new_item(owner, name, "pull_request", title, body)
            item.update(
                head=head,
                base=base,
                draft=draft,
                merged=False,
                reviewers=[],
                reviews=[],
                review_comments=[],
                issue=store.locate(issue_url, "issue").number if issue_url else None,
            )
            store.save(owner, name, item)
        return PullRequestCreated(
            pr_number=item["number"],
            pr_url=_target(owner, name, item).html_url,
            created=created,
            draft=item["draft"],
            head=head,
            base=base,
            target=_target(owner, name, item),
        )

    async def update_pull_request(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        drop_issue_links: bool,
    ) -> PullRequestUpdated:
        ref, item = self._writable(url, "pull_request")
        if drop_issue_links:
            item["issue"] = None
        for key, value in (("body", body), ("title", title)):
            if value is not None:
                item[key] = value
        store.save(ref.owner, ref.repo, item)
        return PullRequestUpdated(
            pr_number=ref.number,
            pr_url=ref.url,
            title=item["title"],
            body=item["body"],
            target=_target(ref.owner, ref.repo, item),
        )

    async def review(self, url: str, body: str, event: ReviewEvent) -> ReviewSubmitted:
        ref, item = self._writable(url, "pull_request")
        review = self._entry(
            ref,
            {
                "body": body.rstrip(),
                "state": _REVIEW_STATES[event],
                "commit_id": self._head_sha(ref, item),
            },
        )
        review["submitted_at"] = review.pop("created_at")
        item["reviews"].append(review)
        store.save(ref.owner, ref.repo, item)
        return ReviewSubmitted(
            review_id=review["id"],
            html_url=None,
            state=review["state"],
            commit_id=review["commit_id"],
            submitted_at=review["submitted_at"],
            target=_target(ref.owner, ref.repo, item),
        )

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
        ref, item = self._writable(url, "pull_request")
        comment = self._entry(
            ref,
            {
                "body": body.rstrip(),
                "path": path,
                "line": line,
                "side": side,
                "start_line": start_line,
                "start_side": start_side,
                "commit_id": self._head_sha(ref, item),
                "in_reply_to": None,
            },
        )
        item["review_comments"].append(comment)
        store.save(ref.owner, ref.repo, item)
        return ReviewCommentWritten(
            review_comment_id=comment["id"],
            html_url=None,
            created_at=comment["created_at"],
            path=path,
            line=line,
            side=side,
            target=_target(ref.owner, ref.repo, item),
        )

    async def reply(self, url: str, reply_target_id: int, body: str) -> ReplyWritten:
        ref, item = self._writable(url, "pull_request")
        root = next(
            (
                comment
                for comment in item["review_comments"]
                if comment["id"] == reply_target_id and comment["in_reply_to"] is None
            ),
            None,
        )
        if root is None:
            raise MemberCapabilityError(
                f"Review comment '{reply_target_id}' is not replyable for this PR."
            )
        reply = self._entry(
            ref,
            {
                **{key: root[key] for key in ("path", "line", "side", "commit_id")},
                "body": body.rstrip(),
                "in_reply_to": reply_target_id,
            },
        )
        item["review_comments"].append(reply)
        store.save(ref.owner, ref.repo, item)
        return ReplyWritten(
            reply_comment_id=reply["id"],
            html_url=None,
            created_at=reply["created_at"],
            target=_target(ref.owner, ref.repo, item),
        )

    async def add_reaction(
        self,
        repo: str,
        target: ReactionTarget,
        comment_id: int,
        reaction: str,
        pr_number: int | None = None,
    ) -> ReactionAdded:
        owner, name = self._writable_repository(repo)
        key = {
            "issue-comment": "comments",
            "pr-review-comment": "review_comments",
            "pr-review": "reviews",
        }[target]
        item, entry = store.find_entry(owner, name, key, comment_id)
        if target == "pr-review" and item["number"] != pr_number:
            raise MemberCapabilityError(
                f"Review {comment_id} is not a review of #{pr_number}."
            )
        entry.setdefault("reactions", []).append(
            {"author": self.person.person_id, "content": reaction}
        )
        store.save(owner, name, item)
        return ReactionAdded(content=reaction, comment_id=comment_id)

    async def readiness(
        self,
        url: str,
        *,
        failed_logs: bool = False,
        log_tail_bytes: int = ReadinessQuery.model_fields["log_tail_bytes"].default,
    ) -> Readiness:
        ReadinessQuery(failed_logs=failed_logs, log_tail_bytes=log_tail_bytes)
        ref = store.locate(url, "pull_request")
        item = store.load(ref.owner, ref.repo, ref.number)
        head_sha = self._head_sha(ref, item)
        succeeds = self._checks() == "success"
        result: dict[str, Any] = {
            "repo": ref.full_repo,
            "pr_number": ref.number,
            "pr_url": ref.url,
            "target": _target(ref.owner, ref.repo, item),
            "head_sha": head_sha,
            "current_head_sha": head_sha,
            "rollup": "success" if succeeds else "failure",
            "checks": [
                {
                    "name": "local",
                    "status": "completed",
                    "conclusion": "success" if succeeds else "failure",
                    "details_url": "",
                    "source": "check_run",
                }
            ],
            "checks_expected": False,
        }
        if failed_logs:
            result["failed_logs"] = []
        if item["state"] != "open":
            return Readiness(
                **result,
                base_sha=None,
                behind_by=None,
                out_of_date=None,
                current_base_sha=None,
                readiness="not_applicable",
                completion_blockers=[],
            )
        base_sha = store.git(
            ref.owner, ref.repo, "rev-parse", f"refs/heads/{item['base']}"
        )
        behind_by = int(
            store.git(
                ref.owner, ref.repo, "rev-list", "--count", f"{head_sha}..{base_sha}"
            )
            or 0
        )
        blockers = []
        if behind_by:
            blockers.append(
                CompletionBlocker(
                    code="base_out_of_date",
                    message=f"The PR head is {behind_by} commit(s) behind its base.",
                    next_action="Merge or rebase the base branch, push, and check again.",
                )
            )
        if not succeeds:
            blockers.append(
                CompletionBlocker(
                    code="checks_failed",
                    message="CI checks are failing.",
                    next_action="Fix the failures and check again.",
                )
            )
        return Readiness(
            **result,
            base_sha=base_sha,
            behind_by=behind_by,
            out_of_date=behind_by > 0,
            current_base_sha=base_sha,
            readiness="blocked" if blockers else "ready",
            completion_blockers=blockers,
        )

    async def open_pull_requests(self, remote_url: str, branch: str) -> list[str]:
        repository = self.repository_from_remote(remote_url)
        if repository is None:
            return []
        owner, repo = repository
        return [
            store.url(owner, repo, "pull_request", item["number"])
            for item in store.items(owner, repo)
            if item["kind"] == "pull_request"
            and item["state"] == "open"
            and item["head"] == branch
        ]

    @asynccontextmanager
    async def artifact(
        self, url: str, name: str, limit: int
    ) -> AsyncIterator[tuple[ArtifactRef, IO[bytes]]]:
        ref = store.locate(url, "pull_request")
        path = store.artifact(ref.owner, ref.repo, ref.number, name)
        if not path.is_file():
            raise MemberCapabilityError(f"Artifact '{name}' was not found for {url}.")
        if path.stat().st_size > limit:
            raise MemberCapabilityError(
                f"Artifact '{name}' is above the {limit} byte limit."
            )
        with path.open("rb") as archive:
            yield (
                ArtifactRef(
                    repo=ref.full_repo,
                    run_id=None,
                    artifact_id=ref.number,
                    artifact_name=name,
                ),
                archive,
            )

    # -- the git remote -----------------------------------------------------

    async def clone_url(self, owner: str, repo: str) -> str:
        return store.bare(owner, repo).as_uri()

    async def default_branch(self, owner: str, repo: str) -> str:
        return store.git(owner, repo, "symbolic-ref", "--short", "HEAD") or "main"

    async def pull_request_head(self, url: str) -> PullRequestHead:
        ref = store.locate(url, "pull_request")
        item = store.load(ref.owner, ref.repo, ref.number)
        owner, repo = store.split(item.get("head_repo") or ref.full_repo)
        return PullRequestHead(owner=owner, repo=repo, branch=item["head"])

    async def push_credential(self) -> str:
        return ""

    def commit_url(self, remote_url: str, sha: str) -> str:
        return ""

    def repository_from_remote(self, remote_url: str) -> tuple[str, str] | None:
        parsed = urlparse(remote_url)
        if parsed.scheme != "file":
            return None
        path = Path(url2pathname(parsed.path))
        if path.parent.parent != store.root() or path.suffix != ".git":
            return None
        return path.parent.name, path.stem

    def remote_host(self, remote_url: str) -> str:
        parsed = urlparse(remote_url)
        if parsed.hostname:
            return parsed.hostname
        if "://" not in remote_url and ":" in remote_url:
            return remote_url.split(":", 1)[0].rsplit("@", 1)[-1]
        return "unrecognized remote"

    # -- the pull request patrol --------------------------------------------

    async def pull_request_candidates(self) -> list[Task]:
        return [
            task
            for owner, repo in store.repositories()
            for item in store.items(owner, repo)
            if (task := self._pull_request_work(owner, repo, item)) is not None
        ]

    async def refresh_pull_request(self, task: Task) -> Task | None:
        assert task.pull_request_url
        ref = store.locate(task.pull_request_url, "pull_request")
        return self._pull_request_work(
            ref.owner, ref.repo, store.load(ref.owner, ref.repo, ref.number)
        )

    def _pull_request_work(
        self, owner: str, repo: str, item: dict[str, Any]
    ) -> Task | None:
        """Feedback the author has not answered, or a requested review of a
        head the member has not reviewed."""
        if item["kind"] != "pull_request" or item["state"] != "open" or item["draft"]:
            return None
        me = self.person.person_id
        said = sorted(
            (
                entry
                for key in ("comments", "reviews", "review_comments")
                for entry in item.get(key, [])
            ),
            key=lambda entry: entry.get("created_at") or entry["submitted_at"],
        )
        if item["author"] == me:
            work = _FEEDBACK if said and said[-1]["author"] != me else None
        else:
            head = _head_sha(owner, repo, item)
            reviewed = {r["commit_id"] for r in item["reviews"] if r["author"] == me}
            work = _REVIEW if me in item["reviewers"] and head not in reviewed else None
        if work is None:
            return None
        return store.task(
            owner,
            repo,
            item,
            Task.IN_PROGRESS,
            assignee=me,
            pull_request_url=_target(owner, repo, item).html_url,
            trigger_reason=work,
        )

    # -- writes -------------------------------------------------------------

    def _writable_repository(self, repo: str) -> tuple[str, str]:
        owner, name = store.split(repo)
        check_repository(configured_owner(self.team.project), owner, name)
        return owner, name

    def _writable(
        self, url: str, kind: ItemKind | None
    ) -> tuple[ItemRef, dict[str, Any]]:
        ref = store.locate(url, kind)
        check_repository(configured_owner(self.team.project), ref.owner, ref.repo)
        return ref, store.load(ref.owner, ref.repo, ref.number)

    def _new_item(
        self, owner: str, repo: str, kind: ItemKind, title: str, body: str
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "number": store.next_number(owner, repo),
            "title": title,
            "body": body,
            "state": "open",
            "author": self.person.person_id,
            "labels": [],
            "assignees": [],
            "lane": None,
            "created_at": store.now(),
            "closed_at": None,
            "comments": [],
        }

    def _entry(self, ref: ItemRef, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": store.next_id(ref.owner, ref.repo),
            "author": self.person.person_id,
            "created_at": store.now(),
            **fields,
        }

    def _defined_labels(
        self, owner: str, repo: str, labels: Sequence[str]
    ) -> list[str]:
        defined = store.repository(owner, repo).get("labels", [])
        undefined = [label for label in labels if label not in defined]
        if undefined:
            raise MemberCapabilityError(
                f"Label '{undefined[0]}' is not defined in {owner}/{repo}. "
                f"Defined labels: {', '.join(defined) or '(none)'}. "
                "Ask a human to add the label instead of introducing a new one."
            )
        return list(dict.fromkeys(labels))

    def _head_sha(self, ref: ItemRef, item: dict[str, Any]) -> str:
        sha = _head_sha(ref.owner, ref.repo, item)
        if not sha:
            raise MemberCapabilityError(
                f"Pull request head commit not found for {ref.full_repo}#{ref.number}."
            )
        return sha

    def _checks(self) -> str:
        config = self.team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        return str(config.get("checks") or "success")


def _target(owner: str, repo: str, item: dict[str, Any]) -> WorkTarget:
    return WorkTarget(
        kind=item["kind"],
        repo=f"{owner}/{repo}",
        number=item["number"],
        title=item["title"],
        html_url=store.url(owner, repo, item["kind"], item["number"]),
    )


def _threads(item: dict[str, Any]) -> list[tuple[dict, list[dict]]]:
    comments = item.get("review_comments", [])
    return [
        (root, [root, *(c for c in comments if c["in_reply_to"] == root["id"])])
        for root in comments
        if root["in_reply_to"] is None
    ]


def _head_sha(owner: str, repo: str, item: dict[str, Any]) -> str:
    """The commit the pull request's head branch is at; empty if it is gone."""
    head_owner, head_repo = store.split(item.get("head_repo") or f"{owner}/{repo}")
    return store.git(head_owner, head_repo, "rev-parse", f"refs/heads/{item['head']}")
