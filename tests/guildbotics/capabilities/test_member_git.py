from collections.abc import Callable
from pathlib import Path

import git
import httpx
import pytest

from guildbotics.capabilities import member_git
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    GitHubPullRequestHead,
    GitHubResource,
    MemberCapabilityError,
)
from guildbotics.entities.team import Person, Project, Team
from tests.git_seed import WorkerGitSeed


@pytest.fixture(autouse=True)
def close_test_repositories(monkeypatch: pytest.MonkeyPatch):
    """Close every GitPython repository created by a test.

    GitPython keeps ``git cat-file --batch`` processes behind repository
    objects. Explicit cleanup is required on Windows, where finalizing a stale
    process after its native handle closes raises ``WinError 6``.
    """
    repositories: list[git.Repo] = []
    original_init = git.Repo.__init__

    def tracked_init(repo, *args, **kwargs):
        original_init(repo, *args, **kwargs)
        repositories.append(repo)

    monkeypatch.setattr(git.Repo, "__init__", tracked_init)
    yield
    for repo in reversed(repositories):
        repo.close()


def _team(person: Person) -> Team:
    return Team(project=Project(name="demo"), members=[person])


def _person() -> Person:
    return Person(
        person_id="aiko",
        name="Aiko",
        account_info={"git_user": "Aiko Bot", "git_email": "aiko@example.com"},
    )


@pytest.fixture
def workspace_repo(
    tmp_path: Path, worker_git_seed: WorkerGitSeed
) -> Callable[[Path], tuple[git.Repo, Path]]:
    """Copy a fully independent remote and worktree from the worker seed."""

    def create(workspace: Path) -> tuple[git.Repo, Path]:
        remote = tmp_path / "remote.git"
        worker_git_seed.copy(worker_git_seed.member_remote, remote)
        repo_path = workspace / "repo"
        worker_git_seed.copy(worker_git_seed.member_worktree, repo_path)
        repo = git.Repo(repo_path)
        repo.remote("origin").set_url(str(remote))
        return repo, repo_path

    return create


_IN_PROGRESS_GIT_FILES = (
    "MERGE_HEAD",
    "MERGE_MSG",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


def _git_dir_file(repo: git.Repo, name: str) -> Path:
    return Path(repo.git_dir) / name


def _assert_no_in_progress_git_state(repo: git.Repo) -> None:
    leftover = [
        name for name in _IN_PROGRESS_GIT_FILES if _git_dir_file(repo, name).exists()
    ]
    assert leftover == []


def _commit_readme(repo: git.Repo, repo_path: Path, content: str, message: str) -> str:
    (repo_path / "README.md").write_text(content, encoding="utf-8")
    repo.git.add(A=True)
    return repo.index.commit(message).hexsha


def _diverged_readme_branches(repo: git.Repo, repo_path: Path) -> tuple[str, str]:
    repo.git.checkout("-b", "theirs")
    theirs = _commit_readme(repo, repo_path, "theirs\n", "theirs")
    repo.git.checkout("main")
    ours = _commit_readme(repo, repo_path, "ours\n", "ours")
    return ours, theirs


def _resolve_readme_conflict(repo: git.Repo, repo_path: Path) -> None:
    (repo_path / "README.md").write_text("resolved\n", encoding="utf-8")
    repo.git.add("README.md")


def test_member_git_auth_environment_disables_external_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    repo = git.Repo.init(tmp_path / "repo")
    askpass_path = tmp_path / "askpass.sh"
    askpass_path.write_text("askpass", encoding="utf-8")
    monkeypatch.setattr(member_git, "create_git_askpass_script", lambda: askpass_path)

    with member_git._git_auth_environment(repo, "member-token"):
        assert repo.git._environment == {
            "GIT_ASKPASS": str(askpass_path),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_USERNAME": "x-access-token",
            "GIT_PASSWORD": "member-token",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }

    assert not askpass_path.exists()


@pytest.mark.asyncio
async def test_publish_commits_pushes_and_preserves_worktree(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
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
async def test_commit_does_not_push(monkeypatch, tmp_path, workspace_repo):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
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
async def test_commit_without_staged_changes_is_a_no_op(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    head_before = repo.head.commit.hexsha
    # Working-tree edit that the caller never staged: nothing should be committed.
    (repo_path / "README.md").write_text("initial\nunstaged\n", encoding="utf-8")

    result = await service.commit(repo_path, "should be skipped")

    assert result.has_changes is False
    assert result.commit_sha is None
    assert result.status == "nothing_staged"
    assert repo.head.commit.hexsha == head_before


@pytest.mark.asyncio
async def test_commit_during_merge_creates_merge_commit_and_clears_merge_state(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    with pytest.raises(git.GitCommandError):
        repo.git.merge("theirs")
    assert (
        _git_dir_file(repo, "MERGE_HEAD").read_text(encoding="utf-8").strip() == theirs
    )
    _resolve_readme_conflict(repo, repo_path)

    result = await service.commit(repo_path, "merge theirs")

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert [parent.hexsha for parent in created.parents] == [ours, theirs]
    assert created.author.email == "aiko@example.com"
    assert created.committer.email == "aiko@example.com"
    assert repo.config_reader().get_value("user", "email") == "existing@example.com"
    _assert_no_in_progress_git_state(repo)


@pytest.mark.asyncio
async def test_commit_during_merge_with_ours_tree_creates_merge_commit(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    with pytest.raises(git.GitCommandError):
        repo.git.merge("theirs")
    (repo_path / "README.md").write_text("ours\n", encoding="utf-8")
    repo.git.add("README.md")
    assert not repo.index.diff(repo.head.commit)

    result = await service.commit(repo_path, "keep ours")

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert [parent.hexsha for parent in created.parents] == [ours, theirs]
    assert created.tree.hexsha == repo.commit(ours).tree.hexsha
    _assert_no_in_progress_git_state(repo)


@pytest.mark.asyncio
async def test_commit_succeeds_when_repository_requires_gpg_sign(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    with repo.config_writer() as writer:
        writer.set_value("commit", "gpgsign", "true")
    (repo_path / "README.md").write_text("initial\nsigned-config\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await service.commit(repo_path, "unsigned member commit")

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert created.author.email == "aiko@example.com"
    assert not created.gpgsig


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cherry-pick", "revert"])
async def test_commit_consumes_sequencer_head_after_conflict_resolution(
    monkeypatch, tmp_path, operation, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    sequencer_file = "CHERRY_PICK_HEAD" if operation == "cherry-pick" else "REVERT_HEAD"
    with pytest.raises(git.GitCommandError):
        if operation == "cherry-pick":
            repo.git.cherry_pick(theirs)
        else:
            repo.git.revert(theirs)
    assert _git_dir_file(repo, sequencer_file).exists()
    _resolve_readme_conflict(repo, repo_path)

    result = await service.commit(repo_path, f"finish {operation}")

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert [parent.hexsha for parent in created.parents] == [ours]
    # Cherry-pick keeps the original author; revert authors the new commit.
    # The committer is the member in both cases.
    if operation == "cherry-pick":
        assert created.author.email == "existing@example.com"
    else:
        assert created.author.email == "aiko@example.com"
    assert created.committer.email == "aiko@example.com"
    _assert_no_in_progress_git_state(repo)


@pytest.mark.asyncio
async def test_push_pushes_existing_commit(monkeypatch, tmp_path, workspace_repo):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
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
async def test_push_without_local_commits_reports_up_to_date(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    workspace_repo(workspace)

    result = await service.push(workspace / "repo")

    assert result.pushed is False
    assert result.status == "up_to_date"
    assert result.commits == []
    assert result.pull_requests == []
    assert result.pull_requests_error is None


@pytest.mark.asyncio
async def test_push_includes_readiness_for_open_prs_on_the_branch(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    _, repo_path = workspace_repo(workspace)
    calls = {}

    async def fake_open_pr_checks(remote_url, branch):
        calls.update({"remote_url": remote_url, "branch": branch})
        return [
            {
                "pr_url": "https://github.com/owner/repo/pull/7",
                "readiness": "blocked",
                "completion_blockers": [{"code": "base_out_of_date"}],
            }
        ]

    monkeypatch.setattr(service.github, "open_pr_checks", fake_open_pr_checks)

    result = await service.push(repo_path)

    assert calls["branch"] == "main"
    assert calls["remote_url"].endswith("remote.git")
    assert result.pull_requests[0]["readiness"] == "blocked"
    assert result.to_dict()["pull_requests"][0]["completion_blockers"] == [
        {"code": "base_out_of_date"}
    ]
    assert "pull_requests_error" not in result.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        MemberCapabilityError("GitHub API request failed with status 502."),
        httpx.ConnectError("GitHub API connection failed."),
    ],
)
async def test_push_stays_successful_when_pr_readiness_lookup_fails(
    monkeypatch, tmp_path, failure, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    (repo_path / "README.md").write_text("initial\ncommitted\n", encoding="utf-8")
    repo.git.add(A=True)
    commit_sha = repo.index.commit("local commit").hexsha

    async def failed_open_pr_checks(_remote_url, _branch):
        raise failure

    monkeypatch.setattr(service.github, "open_pr_checks", failed_open_pr_checks)

    result = await service.push(repo_path)

    assert result.pushed is True
    assert result.commits[0]["id"] == commit_sha
    assert result.pull_requests == []
    assert result.pull_requests_error == str(failure)
    assert result.to_dict()["pull_requests_error"] == result.pull_requests_error
    assert git.Repo(tmp_path / "remote.git").commit("main").hexsha == commit_sha


@pytest.mark.asyncio
async def test_push_rejected_by_remote_raises(monkeypatch, tmp_path, workspace_repo):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    # A second clone advances the remote first, so the member's push is the
    # non-fast-forward the remote refuses.
    other_path = tmp_path / "other"
    other = git.Repo.clone_from(str(tmp_path / "remote.git"), other_path, branch="main")
    with other.config_writer() as writer:
        writer.set_value("user", "name", "Other")
        writer.set_value("user", "email", "other@example.com")
    (other_path / "README.md").write_text("initial\nremote\n", encoding="utf-8")
    other.git.add(A=True)
    remote_sha = other.index.commit("remote commit").hexsha
    other.git.push("origin", "main")
    (repo_path / "README.md").write_text("initial\nlocal\n", encoding="utf-8")
    repo.git.add(A=True)
    local_sha = repo.index.commit("local commit").hexsha

    with pytest.raises(MemberCapabilityError) as excinfo:
        await service.push(repo_path)

    message = str(excinfo.value)
    assert "Failed to push 'main' to origin" in message
    assert "non-fast-forward" in message
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha == remote_sha
    assert remote_repo.commit("main").hexsha != local_sha


@pytest.mark.asyncio
async def test_push_failing_hard_raises_with_git_error_as_cause(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    repo, repo_path = workspace_repo(workspace)
    (repo_path / "README.md").write_text("initial\nlocal\n", encoding="utf-8")
    repo.git.add(A=True)
    local_sha = repo.index.commit("local commit").hexsha
    # git can fail before it reports any ref status, and then Remote.push raises
    # instead of returning a PushInfoList carrying the rejection.
    failure = git.GitCommandError(
        ["git", "push", "origin", "main"], 128, b"fatal: remote hung up unexpectedly"
    )

    def fail_push(self, *args, **kwargs):
        raise failure

    monkeypatch.setattr(git.Remote, "push", fail_push)

    with pytest.raises(MemberCapabilityError) as excinfo:
        await service.push(repo_path)

    message = str(excinfo.value)
    assert "Failed to push 'main' to origin" in message
    assert "remote hung up unexpectedly" in message
    assert excinfo.value.__cause__ is failure
    remote_repo = git.Repo(tmp_path / "remote.git")
    assert remote_repo.commit("main").hexsha != local_sha


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
async def test_prepare_branch_mode_checks_out_ad_hoc_branch(monkeypatch, tmp_path):
    # Chat-originated work has no issue: prepare must work from just a
    # repository and a branch name.
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    calls = {}

    class FakeGitHub:
        base_url = "https://api.github.com"

        def parse_url(self, url):
            raise AssertionError("parse_url must not be called in branch mode")

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
            calls["repo_url"] = repo_url
            self.repo_path = workspace / "repo"

        def checkout_branch(self, branch):
            calls["checkout_branch"] = branch

        def close(self):
            calls["closed"] = True

    async def fake_token(person, base_url):
        return "token"

    service.github = FakeGitHub()
    monkeypatch.setattr(member_git, "GitTool", FakeGitTool)
    monkeypatch.setattr(member_git, "get_person_github_token", fake_token)

    result = await service.prepare(repo="acme/widget", branch="chat/fix-typo")

    assert calls["default_branch_repo"] == ("acme", "widget")
    assert calls["clone_repo"] == ("acme", "widget")
    assert calls["checkout_branch"] == "chat/fix-typo"
    assert result["repo"] == "acme/widget"
    assert result["checkout_repo"] == "acme/widget"
    assert result["branch"] == "chat/fix-typo"
    assert result["mode"] == "branch"
    assert result["issue_url"] == ""
    assert result["pr_url"] == ""


@pytest.mark.asyncio
async def test_prepare_requires_an_anchor():
    service = MemberGitWorkspaceService(_person(), _team(_person()))

    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await service.prepare()
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await service.prepare(repo="acme/widget")
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await service.prepare(branch="chat/fix-typo")


@pytest.mark.asyncio
async def test_prepare_rejects_malformed_repo():
    service = MemberGitWorkspaceService(_person(), _team(_person()))

    with pytest.raises(MemberCapabilityError, match="<owner>/<repo>"):
        await service.prepare(repo="acme", branch="chat/fix-typo")


@pytest.mark.asyncio
async def test_publish_rejects_repo_outside_member_workspace(tmp_path):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    outside_repo = git.Repo.init(tmp_path / "outside")

    with pytest.raises(MemberCapabilityError, match="member workspace"):
        await service.publish(Path(outside_repo.working_tree_dir or ""), "message")


@pytest.mark.asyncio
async def test_publish_current_workspace_allows_current_repo_outside_member_workspace(
    monkeypatch, tmp_path, workspace_repo
):
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    repo, repo_path = workspace_repo(tmp_path / "current")
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
    tmp_path, workspace_repo
):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    _, current_repo_path = workspace_repo(tmp_path / "current")
    other_repo = git.Repo.init(tmp_path / "other")

    with pytest.raises(MemberCapabilityError, match="current workspace repository"):
        await service.publish_current_workspace(
            Path(other_repo.working_tree_dir or ""), "message", cwd=current_repo_path
        )


@pytest.mark.asyncio
async def test_publish_rejects_empty_commit_message(tmp_path, workspace_repo):
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    workspace = tmp_path / "workspace" / "aiko"
    service.workspace_root = workspace
    _, repo_path = workspace_repo(workspace)

    with pytest.raises(MemberCapabilityError, match="must not be empty"):
        await service.publish(repo_path, "  \n")


@pytest.mark.asyncio
async def test_chat_run_cannot_push_unchecked_work(
    monkeypatch, tmp_path, workspace_repo
):
    from guildbotics.capabilities.chat_updates import (
        ChatUpdatesRequired,
        check_chat_updates,
    )
    from guildbotics.capabilities.task_runs import RunStore
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
    from guildbotics.runtime.member_invocation import (
        MemberInvocation,
        member_invocation_scope,
    )

    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-token")
    RunStore().append_evidence(
        "chat-run",
        "chat_batch",
        {
            "person_id": "aiko",
            "service": "slack",
            "channel_id": "C1",
            "thread_ts": "100.1",
            "self_user_id": "U_BOT",
            "event_ids": ["E1"],
        },
    )
    ChatReceiveStatus().save("slack", "aiko", "C1", state="ready")
    service = MemberGitWorkspaceService(_person(), _team(_person()))
    service.workspace_root = tmp_path / "workspace" / "aiko"
    repo, repo_path = workspace_repo(service.workspace_root)
    (repo_path / "README.md").write_text("changed\n", encoding="utf-8")
    repo.git.add(A=True)
    commit_sha = repo.index.commit("local commit").hexsha
    with member_invocation_scope(MemberInvocation(run_id="chat-run")):
        with pytest.raises(ChatUpdatesRequired):
            await service.push(repo_path)
        with git.Repo(tmp_path / "remote.git") as remote:
            assert remote.commit("main").hexsha != commit_sha
        check_chat_updates("aiko", "chat-run")
        await service.push(repo_path)
    with git.Repo(tmp_path / "remote.git") as remote:
        assert remote.commit("main").hexsha == commit_sha
