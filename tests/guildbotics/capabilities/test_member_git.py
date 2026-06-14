from pathlib import Path

import git
import pytest

from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import MemberCapabilityError
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

    result = await service.publish(repo_path, "publish changes")

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.pushed is True
    assert readme.read_text(encoding="utf-8") == "initial\nchanged\n"
    assert untracked.read_text(encoding="utf-8") == "new\n"
    assert repo.is_dirty(untracked_files=True) is False
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha == result.commit_sha


@pytest.mark.asyncio
async def test_commit_does_not_push(monkeypatch, tmp_path):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)
    (repo_path / "README.md").write_text("initial\ncommitted\n", encoding="utf-8")

    result = await service.commit(repo_path, "commit only")

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.status == "committed"
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").message == "initial"
    assert repo.commit("main").hexsha == result.commit_sha


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
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha == commit_sha


@pytest.mark.asyncio
async def test_create_branch_from_current_branch(tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = _workspace_repo(tmp_path, workspace)

    result = await service.create_branch(repo_path, "feature/test")

    assert result.previous_branch == "main"
    assert result.branch == "feature/test"
    assert result.status == "created"
    assert repo.active_branch.name == "feature/test"


@pytest.mark.asyncio
async def test_create_branch_rejects_existing_branch(tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    _, repo_path = _workspace_repo(tmp_path, workspace)

    with pytest.raises(MemberCapabilityError, match="already exists"):
        await service.create_branch(repo_path, "main")


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
    before_branch = repo.active_branch.name

    result = await service.publish_current_workspace(
        repo_path, "publish current workspace", cwd=repo_path
    )

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.pushed is True
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
