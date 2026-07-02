from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import git
from git import Actor, GitCommandError

from guildbotics.capabilities.member_github import (
    MemberCapabilityError,
    MemberGitHubCapabilityService,
)
from guildbotics.entities.team import Person, Team
from guildbotics.integrations.github.github_utils import get_person_github_token
from guildbotics.utils.fileio import get_workspace_path
from guildbotics.utils.git_tool import GitTool, create_git_askpass_script


@dataclass(frozen=True)
class PublishResult:
    repo_path: str
    branch: str
    commit_sha: str | None
    pushed: bool
    has_changes: bool
    status: str
    commits: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "has_changes": self.has_changes,
            "status": self.status,
            "commits": self.commits,
        }


@dataclass(frozen=True)
class CommitResult:
    repo_path: str
    branch: str
    commit_sha: str | None
    has_changes: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "has_changes": self.has_changes,
            "status": self.status,
        }


@dataclass(frozen=True)
class PushResult:
    repo_path: str
    branch: str
    pushed: bool
    status: str
    commits: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "pushed": self.pushed,
            "status": self.status,
            "commits": self.commits,
        }


class MemberGitWorkspaceService:
    def __init__(
        self, person: Person, team: Team, logger: logging.Logger | None = None
    ) -> None:
        self.person = person
        self.team = team
        self.logger = logger or logging.getLogger(__name__)
        self.github = MemberGitHubCapabilityService(person, team)
        self.workspace_root = get_workspace_path(person.person_id)

    async def aclose(self) -> None:
        await self.github.aclose()

    async def prepare(
        self, issue_url: str, pr_url: str | None = None
    ) -> dict[str, Any]:
        resource = self.github.parse_url(pr_url or issue_url)
        if resource.kind == "pull":
            mode = "pull_request_review"
            pr_url = pr_url or issue_url
            head = await self.github.get_pr_head(pr_url)
            branch = head.branch
            checkout_owner = head.owner
            checkout_repo = head.repo
        else:
            mode = "issue"
            branch = f"ticket/{resource.number}"
            checkout_owner = resource.owner
            checkout_repo = resource.repo
        default_branch = await self.github.default_branch(checkout_owner, checkout_repo)
        clone_url = await self.github.get_clone_url(checkout_owner, checkout_repo)
        token = await get_person_github_token(self.person, self.github.base_url)
        git_user, git_email = self._git_identity()
        tool = GitTool(
            self.workspace_root,
            clone_url,
            self.logger,
            git_user,
            git_email,
            default_branch,
            auth_token=token,
        )
        try:
            tool.checkout_branch(branch)
            repo_path = tool.repo_path
        finally:
            tool.close()
        return {
            "repo": resource.full_repo,
            "checkout_repo": f"{checkout_owner}/{checkout_repo}",
            "repo_path": str(repo_path),
            "branch": branch,
            "default_branch": default_branch,
            "issue_url": issue_url,
            "pr_url": pr_url or "",
            "mode": mode,
        }

    async def publish(self, repo_path: Path, message: str) -> PublishResult:
        return await self._publish(repo_path, message, workspace_mode="member")

    async def publish_current_workspace(
        self, repo_path: Path, message: str, cwd: Path | None = None
    ) -> PublishResult:
        return await self._publish(
            repo_path, message, workspace_mode="current", cwd=cwd
        )

    async def commit(
        self,
        repo_path: Path,
        message: str,
        *,
        workspace_mode: Literal["member", "current"] = "member",
        cwd: Path | None = None,
    ) -> CommitResult:
        repo_path = repo_path.expanduser().resolve()
        self._validate_repo_path(repo_path, workspace_mode=workspace_mode, cwd=cwd)
        if not message.strip():
            raise MemberCapabilityError("Commit message must not be empty.")
        repo = git.Repo(repo_path)
        branch = repo.active_branch.name
        has_staged = self._has_staged_changes(repo)
        commit_sha = None
        if has_staged:
            actor = self._member_actor()
            commit_sha = repo.index.commit(
                message.strip(), author=actor, committer=actor
            ).hexsha
        return CommitResult(
            repo_path=str(repo_path),
            branch=branch,
            commit_sha=commit_sha,
            has_changes=has_staged,
            status="committed" if commit_sha else "nothing_staged",
        )

    async def push(
        self,
        repo_path: Path,
        *,
        workspace_mode: Literal["member", "current"] = "member",
        cwd: Path | None = None,
    ) -> PushResult:
        repo_path = repo_path.expanduser().resolve()
        self._validate_repo_path(repo_path, workspace_mode=workspace_mode, cwd=cwd)
        repo = git.Repo(repo_path)
        branch = repo.active_branch.name
        token = await get_person_github_token(self.person, self.github.base_url)
        pushed, commits = self._push_if_needed(repo, branch, token)
        return PushResult(
            repo_path=str(repo_path),
            branch=branch,
            pushed=pushed,
            status="pushed" if pushed else "up_to_date",
            commits=commits,
        )

    async def _publish(
        self,
        repo_path: Path,
        message: str,
        *,
        workspace_mode: Literal["member", "current"],
        cwd: Path | None = None,
    ) -> PublishResult:
        commit = await self.commit(
            repo_path, message, workspace_mode=workspace_mode, cwd=cwd
        )
        push = await self.push(repo_path, workspace_mode=workspace_mode, cwd=cwd)
        return PublishResult(
            repo_path=commit.repo_path,
            branch=commit.branch,
            commit_sha=commit.commit_sha,
            pushed=push.pushed,
            has_changes=commit.has_changes,
            status=(
                "published" if (commit.commit_sha or push.pushed) else "up_to_date"
            ),
            commits=push.commits,
        )

    def _validate_repo_path(
        self,
        repo_path: Path,
        *,
        workspace_mode: Literal["member", "current"],
        cwd: Path | None = None,
    ) -> None:
        if workspace_mode == "current":
            self._validate_current_workspace_repo(repo_path, cwd or Path.cwd())
            return

        workspace = self.workspace_root.expanduser().resolve()
        if repo_path != workspace and workspace not in repo_path.parents:
            raise MemberCapabilityError(
                f"repo_path must be under member workspace root: {workspace}"
            )
        if not (repo_path / ".git").exists():
            raise MemberCapabilityError(
                f"repo_path is not a git repository: {repo_path}"
            )

    def _validate_current_workspace_repo(self, repo_path: Path, cwd: Path) -> None:
        if not (repo_path / ".git").exists():
            raise MemberCapabilityError(
                f"repo_path is not a git repository: {repo_path}"
            )
        try:
            current_repo = git.Repo(
                cwd.expanduser().resolve(), search_parent_directories=True
            )
        except git.InvalidGitRepositoryError as exc:
            raise MemberCapabilityError(
                "current workspace mode requires running inside the target git repository."
            ) from exc
        current_root = Path(current_repo.working_tree_dir or "").resolve()
        if repo_path != current_root:
            raise MemberCapabilityError(
                f"repo_path must match the current workspace repository: {current_root}"
            )

    def _git_identity(self) -> tuple[str, str]:
        git_user = str(
            self.person.account_info.get("git_user", self.person.name or "GuildBotics")
        )
        git_email = str(
            self.person.account_info.get(
                "git_email", f"{self.person.person_id}@guildbotics.local"
            )
        )
        return git_user, git_email

    def _member_actor(self) -> Actor:
        git_user, git_email = self._git_identity()
        return Actor(git_user, git_email)

    @staticmethod
    def _has_staged_changes(repo: git.Repo) -> bool:
        # Commit only what the caller already staged with plain git. The member
        # capability never stages on the caller's behalf, so that staging stays a
        # normal git operation and partial commits remain possible.
        if not repo.head.is_valid():
            return bool(repo.index.entries)
        return bool(repo.index.diff(repo.head.commit))

    def _push_if_needed(
        self, repo: git.Repo, branch: str, token: str
    ) -> tuple[bool, list[dict[str, str]]]:
        origin = repo.remotes.origin
        with _git_auth_environment(repo, token):
            origin.fetch()
        try:
            commits_ahead = list(repo.iter_commits(f"origin/{branch}..{branch}"))
            need_push = bool(commits_ahead)
        except GitCommandError:
            commits_ahead = [repo.head.commit] if repo.head.is_valid() else []
            need_push = True
        if not need_push:
            return False, []
        commits = [
            _commit_summary(
                commit, self.github.commit_url_from_remote(origin.url, commit.hexsha)
            )
            for commit in commits_ahead
        ]
        with _git_auth_environment(repo, token):
            origin.push(branch)
        return True, commits


def _commit_summary(commit: git.Commit, url: str) -> dict[str, str]:
    message = str(commit.message or "").strip()
    return {
        "id": commit.hexsha,
        "message": message.splitlines()[0] if message else commit.hexsha[:7],
        "url": url,
    }


@contextmanager
def _git_auth_environment(repo: git.Repo, token: str) -> Iterator[None]:
    if not token:
        yield
        return
    askpass_path: Path | None = None
    try:
        askpass_path = create_git_askpass_script()
        with repo.git.custom_environment(
            GIT_ASKPASS=str(askpass_path),
            GIT_TERMINAL_PROMPT="0",
            GIT_USERNAME="x-access-token",
            GIT_PASSWORD=token,
        ):
            yield
    finally:
        if askpass_path is not None:
            askpass_path.unlink(missing_ok=True)
