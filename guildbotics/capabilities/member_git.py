from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import git
from git import GitCommandError

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "has_changes": self.has_changes,
            "status": self.status,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "pushed": self.pushed,
            "status": self.status,
        }


@dataclass(frozen=True)
class BranchResult:
    repo_path: str
    branch: str
    previous_branch: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "previous_branch": self.previous_branch,
            "status": self.status,
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
            branch = await self.github.get_pr_head_branch(pr_url)
        else:
            mode = "issue"
            branch = f"ticket/{resource.number}"
        default_branch = await self.github.default_branch(resource.owner, resource.repo)
        clone_url = await self.github.get_clone_url(resource.owner, resource.repo)
        token = await get_person_github_token(self.person, self.github.base_url)
        git_user = self.person.account_info.get(
            "git_user", self.person.name or "GuildBotics"
        )
        git_email = self.person.account_info.get(
            "git_email", f"{self.person.person_id}@guildbotics.local"
        )
        tool = GitTool(
            self.workspace_root,
            clone_url,
            self.logger,
            str(git_user),
            str(git_email),
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
        self._configure_repo_identity(repo)
        branch = repo.active_branch.name
        has_changes = repo.is_dirty(untracked_files=True)
        commit_sha = None
        if has_changes:
            repo.git.add(A=True)
            commit_sha = repo.index.commit(message.strip()).hexsha
        return CommitResult(
            repo_path=str(repo_path),
            branch=branch,
            commit_sha=commit_sha,
            has_changes=has_changes,
            status="committed" if commit_sha else "up_to_date",
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
        pushed = self._push_if_needed(repo, branch, token)
        return PushResult(
            repo_path=str(repo_path),
            branch=branch,
            pushed=pushed,
            status="pushed" if pushed else "up_to_date",
        )

    async def create_branch(
        self,
        repo_path: Path,
        branch: str,
        *,
        workspace_mode: Literal["member", "current"] = "member",
        cwd: Path | None = None,
    ) -> BranchResult:
        repo_path = repo_path.expanduser().resolve()
        self._validate_repo_path(repo_path, workspace_mode=workspace_mode, cwd=cwd)
        branch = branch.strip()
        if not branch:
            raise MemberCapabilityError("Branch name must not be empty.")
        repo = git.Repo(repo_path)
        previous_branch = repo.active_branch.name
        if branch in {head.name for head in repo.heads}:
            raise MemberCapabilityError(f"Branch already exists: {branch}")
        repo.git.checkout("-b", branch)
        return BranchResult(
            repo_path=str(repo_path),
            branch=branch,
            previous_branch=previous_branch,
            status="created",
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

    def _configure_repo_identity(self, repo: git.Repo) -> None:
        git_user = str(
            self.person.account_info.get("git_user", self.person.name or "GuildBotics")
        )
        git_email = str(
            self.person.account_info.get(
                "git_email", f"{self.person.person_id}@guildbotics.local"
            )
        )
        with repo.config_writer(config_level="repository") as writer:
            writer.set_value("user", "name", git_user)
            writer.set_value("user", "email", git_email)

    def _push_if_needed(self, repo: git.Repo, branch: str, token: str) -> bool:
        origin = repo.remotes.origin
        with _git_auth_environment(repo, token):
            origin.fetch()
        try:
            commits_ahead = list(repo.iter_commits(f"origin/{branch}..{branch}"))
            need_push = bool(commits_ahead)
        except GitCommandError:
            need_push = True
        if not need_push:
            return False
        with _git_auth_environment(repo, token):
            origin.push(branch)
        return True


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
