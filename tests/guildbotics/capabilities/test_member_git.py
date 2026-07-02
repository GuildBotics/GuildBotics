from pathlib import Path

import git
import pytest

from guildbotics.capabilities import member_git
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    GitHubPullRequestHead,
    GitHubResource,
    MemberCapabilityError,
)
from guildbotics.entities.team import Person, Project, Team


def _team(person: Person) -> Team:
    return Team(project=Project(name="demo"), members=[person])


def _person() -> Person:
    return Person(
        person_id="aiko",
        name="Aiko",
        account_info={"git_user": "Aiko Bot", "git_email": "aiko@example.com"},
    )


def _seed_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    git.Repo.init(remote, bare=True)
    seed_path = tmp_path / "seed"
    seed = git.Repo.init(seed_path)
    with seed.config_writer() as writer:
        writer.set_value("user", "name", "Seed")
        writer.set_value("user", "email", "seed@example.com")
    (seed_path / "README.md").write_text("initial\n", encoding="utf-8")
    seed.git.add(A=True)
    seed.index.commit("initial")
    try:
        seed.git.checkout("main")
    except git.GitCommandError:
        seed.git.checkout("-b", "main")
    seed.create_remote("origin", str(remote))
    seed.git.push("--set-upstream", "origin", "main")
    return remote


def _workspace_repo(tmp_path: Path, workspace: Path) -> tuple[git.Repo, Path]:
    remote = _seed_remote(tmp_path)
    repo_path = workspace / "repo"
    repo = git.Repo.clone_from(str(remote), repo_path, branch="main")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Existing")
        writer.set_value("user", "email", "existing@example.com")
    return repo, repo_path


@pytest.mark.asyncio
async def test_publish_commits_pushes_and_preserves_worktree(monkeypatch, tmp_path):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)
    readme = repo_path / "README.md"
    readme.write_text("initial\nchanged\n", encoding="utf-8")
    untracked = repo_path / "new.txt"
    untracked.write_text("new\n", encoding="utf-8")
    # Staging is a plain git step the caller performs; the member capability
    # only commits what is already staged.
    repo.git.add(A=True)

    result = await service.publish(repo_path, "publish changes")

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.pushed is True
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish changes", "url": ""}
    ]
    assert readme.read_text(encoding="utf-8") == "initial\nchanged\n"
    assert untracked.read_text(encoding="utf-8") == "new\n"
    assert repo.is_dirty(untracked_files=True) is False
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha == result.commit_sha
    # The commit carries the member identity, not the repository's configured user.
    published = repo.commit(result.commit_sha)
    assert published.author.email == "aiko@example.com"
    assert published.committer.email == "aiko@example.com"


@pytest.mark.asyncio
async def test_commit_does_not_push(monkeypatch, tmp_path):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)
    (repo_path / "README.md").write_text("initial\ncommitted\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await service.commit(repo_path, "commit only")

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.status == "committed"
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").message == "initial"
    assert repo.commit("main").hexsha == result.commit_sha
    # The member identity is applied to the commit itself; the repository's git
    # config is left untouched so a later interactive commit keeps the user's own
    # identity.
    assert repo.commit(result.commit_sha).author.email == "aiko@example.com"
    assert repo.config_reader().get_value("user", "email") == "existing@example.com"


@pytest.mark.asyncio
async def test_commit_without_staged_changes_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)
    head_before = repo.head.commit.hexsha
    # Working-tree edit that the caller never staged: nothing should be committed.
    (repo_path / "README.md").write_text("initial\nunstaged\n", encoding="utf-8")

    result = await service.commit(repo_path, "should be skipped")

    assert result.has_changes is False
    assert result.commit_sha is None
    assert result.status == "nothing_staged"
    assert repo.head.commit.hexsha == head_before


@pytest.mark.asyncio
async def test_push_pushes_existing_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)
    (repo_path / "README.md").write_text("initial\ncommitted\n", encoding="utf-8")
    repo.git.add(A=True)
    commit_sha = repo.index.commit("local commit").hexsha

    result = await service.push(repo_path)

    assert result.pushed is True
    assert result.status == "pushed"
    assert result.commits == [{"id": commit_sha, "message": "local commit", "url": ""}]
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha == commit_sha


@pytest.mark.asyncio
async def test_prepare_pull_request_review_clones_fork_head_repo(monkeypatch, tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    calls = {}

    class FakeGitHub:
        base_url = "https://api.github.com"

        def parse_url(self, url):
            calls["parsed_url"] = url
            return GitHubResource("owner", "repo", 7, "pull")

        async def get_pr_head(self, url):
            calls["head_url"] = url
            return GitHubPullRequestHead("contributor", "repo", "feature")

        async def default_branch(self, owner, repo):
            calls["default_branch_repo"] = (owner, repo)
            return "main"

        async def get_clone_url(self, owner, repo):
            calls["clone_repo"] = (owner, repo)
            return f"https://github.com/{owner}/{repo}.git"

    class FakeGitTool:
        def __init__(
            self,
            workspace,
            repo_url,
            logger,
            user_name,
            user_email,
            default_branch,
            auth_token=None,
        ):
            calls["git_tool"] = {
                "workspace": workspace,
                "repo_url": repo_url,
                "default_branch": default_branch,
                "auth_token": auth_token,
            }
            self.repo_path = workspace / "repo"

        def checkout_branch(self, branch):
            calls["checkout_branch"] = branch

        def close(self):
            calls["closed"] = True

    async def fake_token(person, base_url):
        calls["token_base_url"] = base_url
        return "token"

    service.github = FakeGitHub()
    monkeypatch.setattr(member_git, "GitTool", FakeGitTool)
    monkeypatch.setattr(member_git, "get_person_github_token", fake_token)

    result = await service.prepare(
        "https://github.com/owner/repo/issues/42",
        pr_url="https://github.com/owner/repo/pull/7",
    )

    assert calls["default_branch_repo"] == ("contributor", "repo")
    assert calls["clone_repo"] == ("contributor", "repo")
    assert calls["git_tool"]["repo_url"] == "https://github.com/contributor/repo.git"
    assert calls["checkout_branch"] == "feature"
    assert result["repo"] == "owner/repo"
    assert result["checkout_repo"] == "contributor/repo"
    assert result["branch"] == "feature"
    assert result["mode"] == "pull_request_review"


@pytest.mark.asyncio
async def test_publish_rejects_repo_outside_member_workspace(tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    outside_repo = git.Repo.init(tmp_path / "outside")

    with pytest.raises(MemberCapabilityError, match="member workspace"):
        await service.publish(Path(outside_repo.working_tree_dir or ""), "message")


@pytest.mark.asyncio
async def test_publish_current_workspace_allows_current_repo_outside_member_workspace(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    repo, repo_path = _workspace_repo(tmp_path, tmp_path / "current")
    (repo_path / "README.md").write_text("initial\ncurrent\n", encoding="utf-8")
    repo.git.add(A=True)
    before_branch = repo.active_branch.name

    result = await service.publish_current_workspace(
        repo_path, "publish current workspace", cwd=repo_path
    )

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.pushed is True
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish current workspace", "url": ""}
    ]
    assert repo.active_branch.name == before_branch


@pytest.mark.asyncio
async def test_publish_current_workspace_rejects_repo_that_is_not_current_workspace(
    tmp_path,
):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    _, current_repo_path = _workspace_repo(tmp_path, tmp_path / "current")
    other_repo = git.Repo.init(tmp_path / "other")

    with pytest.raises(MemberCapabilityError, match="current workspace repository"):
        await service.publish_current_workspace(
            Path(other_repo.working_tree_dir or ""), "message", cwd=current_repo_path
        )


@pytest.mark.asyncio
async def test_publish_rejects_empty_commit_message(tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    _, repo_path = _workspace_repo(tmp_path, workspace)

    with pytest.raises(MemberCapabilityError, match="must not be empty"):
        await service.publish(repo_path, "  \n")
