"""Member git: what a member's clone decides runs only in the command's environment.

``_LocalGuest`` stands in for the command's microVM. It runs the clone's git
on this machine, but marks every process it starts (``GUEST_MARK``), so
whatever a clone plants -- a hook, a filter, an fsmonitor, a link to another
repository -- can tell whether it ran for the environment or on the host.
Every process the host itself starts goes through ``subprocess.run``
(``test_member_git_starts_processes_only_through_subprocess_run``), which
``host_git`` records.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import git
import httpx
import pytest

from guildbotics.capabilities import member_git
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    GitHubPullRequestHead,
    MemberCapabilityError,
)
from guildbotics.commands.utils import find_shell
from guildbotics.entities.team import Person, Project, Team
from guildbotics.runtime.member_invocation import (
    GuestProcessError,
    GuestResult,
    MemberInvocation,
    member_invocation_scope,
)
from tests.git_seed import WorkerGitSeed

GUEST_MARK = "GUILDBOTICS_TEST_GUEST"
_REAL_RUN = subprocess.run
#: What the member's git may be given inside the environment.
_GUEST_ENV_KEYS = {
    "GIT_TERMINAL_PROMPT",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
}
_TOKEN = "dummy-token"


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


class _LocalGuest:
    """The command's environment, run on this machine and marked."""

    def __init__(self) -> None:
        self.runs: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def path(self, host: Path) -> str:
        return host.as_posix()

    def remaining(self) -> float:
        return 60.0

    def run(
        self,
        argv,
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        stdout_limit: int,
    ) -> GuestResult:
        self.runs.append((tuple(argv), cwd, dict(env)))
        program = (find_shell() or "sh") if argv[0] == "sh" else argv[0]
        inherited = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        ran = _REAL_RUN(
            [program, *argv[1:]],
            cwd=cwd,
            env={**inherited, **env, GUEST_MARK: "1"},
            input=stdin.read_bytes() if isinstance(stdin, Path) else stdin,
            capture_output=True,
            check=False,
        )
        if len(ran.stdout) > stdout_limit:
            raise GuestProcessError("The command wrote too much output.")
        if stdout is None:
            return GuestResult(ran.returncode, ran.stdout, ran.stderr)
        stdout.write_bytes(ran.stdout)
        return GuestResult(ran.returncode, b"", ran.stderr)


@dataclass(frozen=True)
class _HostCall:
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


@pytest.fixture
def host_git(monkeypatch: pytest.MonkeyPatch) -> list[_HostCall]:
    """Every process the host starts, as it starts it."""
    calls: list[_HostCall] = []

    def run(args, **kwargs):
        calls.append(
            _HostCall(tuple(args), Path(kwargs["cwd"]), dict(kwargs.get("env") or {}))
        )
        return _REAL_RUN(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _person() -> Person:
    return Person(
        person_id="aiko",
        name="Aiko",
        account_info={"git_user": "Aiko Bot", "git_email": "aiko@example.com"},
    )


@dataclass
class _Member:
    """A member with a command's environment, and the remotes it reaches.

    ``remotes[owner]`` is the ``<owner>/repo`` repository of the code host.
    """

    service: MemberGitWorkspaceService
    guest: _LocalGuest
    remotes: dict[str, Path]
    heads: dict[str, GitHubPullRequestHead] = field(default_factory=dict)

    @property
    def clone(self) -> Path:
        return self.service.workspace_root / "repo"

    def url(self, owner: str) -> str:
        return self.remotes[owner].as_uri()

    @property
    def origin(self) -> Path:
        return self.service.host_root / "repositories"

    def run(self, operation: Callable[[MemberGitWorkspaceService], object]):
        async def invoke():
            with member_invocation_scope(MemberInvocation(guest=self.guest)):
                return await operation(self.service)  # type: ignore[misc]

        return invoke()

    async def prepare(self, **anchor: str) -> dict:
        anchor = anchor or {"issue_url": "https://github.com/owner/repo/issues/1"}
        return await self.run(lambda service: service.prepare(**anchor))

    def repo(self) -> git.Repo:
        return git.Repo(self.clone)

    def remote(self, owner: str = "owner") -> git.Repo:
        return git.Repo(self.remotes[owner])

    def stage(self, name: str = "README.md", content: str = "changed\n") -> git.Repo:
        (self.clone / name).write_text(content, encoding="utf-8")
        repo = self.repo()
        repo.git.add(A=True)
        return repo


@pytest.fixture
def member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_git_seed: WorkerGitSeed
) -> _Member:
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", _TOKEN)
    remotes: dict[str, Path] = {}
    for owner in ("owner", "contributor"):
        remotes[owner] = tmp_path / "remotes" / owner / "repo.git"
        worker_git_seed.copy(worker_git_seed.member_remote, remotes[owner])
    service = MemberGitWorkspaceService(
        _person(), Team(project=Project(name="demo"), members=[_person()])
    )
    # The command's turns work in it: it is there before any of them runs.
    service.workspace_root.mkdir(parents=True)
    result = _Member(service, _LocalGuest(), remotes)

    async def default_branch(owner, repo):
        return "main"

    async def clone_url(owner, repo):
        return result.url(owner)

    async def pr_head(url):
        return result.heads[url]

    monkeypatch.setattr(service.github, "default_branch", default_branch)
    monkeypatch.setattr(service.github, "get_clone_url", clone_url)
    monkeypatch.setattr(service.github, "get_pr_head", pr_head)
    return result


@pytest.fixture
def seeded(
    member: _Member, worker_git_seed: WorkerGitSeed
) -> Callable[[], tuple[git.Repo, Path]]:
    """A clone as a turn left it, with the seed's own identity configured."""

    def create() -> tuple[git.Repo, Path]:
        worker_git_seed.copy(worker_git_seed.member_worktree, member.clone)
        repo = member.repo()
        repo.remote("origin").set_url(member.url("owner"))
        return repo, member.clone

    return create


_IN_PROGRESS_GIT_FILES = (
    "MERGE_HEAD",
    "MERGE_MSG",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


def _assert_no_in_progress_git_state(repo: git.Repo) -> None:
    leftover = [
        name for name in _IN_PROGRESS_GIT_FILES if (Path(repo.git_dir) / name).exists()
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


def _advance(remote: Path, tmp_path: Path, content: str) -> str:
    """Commit to ``remote``'s main from a clone of its own; the new sha."""
    other_path = tmp_path / f"other-{content.strip()}"
    other = git.Repo.clone_from(str(remote), other_path, branch="main")
    with other.config_writer() as writer:
        writer.set_value("user", "name", "Other")
        writer.set_value("user", "email", "other@example.com")
    sha = _commit_readme(other, other_path, content, f"remote {content.strip()}")
    other.git.push("origin", "main")
    return sha


# -- commit ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_does_not_push(member, seeded):
    repo, repo_path = seeded()
    (repo_path / "README.md").write_text("initial\ncommitted\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await member.run(lambda s: s.commit(repo_path, "commit only"))

    assert result.has_changes is True
    assert result.commit_sha is not None
    assert result.status == "committed"
    assert result.branch == "main"
    assert member.remote().commit("main").message == "initial"
    assert repo.commit("main").hexsha == result.commit_sha
    # The member identity is applied to the commit itself; the repository's git
    # config is left untouched so a later interactive commit keeps the user's own
    # identity.
    assert repo.commit(result.commit_sha).author.email == "aiko@example.com"
    assert repo.config_reader().get_value("user", "email") == "existing@example.com"


@pytest.mark.asyncio
async def test_commit_without_staged_changes_is_a_no_op(member, seeded):
    repo, repo_path = seeded()
    head_before = repo.head.commit.hexsha
    # Working-tree edit that the caller never staged: nothing should be committed.
    (repo_path / "README.md").write_text("initial\nunstaged\n", encoding="utf-8")

    result = await member.run(lambda s: s.commit(repo_path, "should be skipped"))

    assert result.has_changes is False
    assert result.commit_sha is None
    assert result.status == "nothing_staged"
    assert repo.head.commit.hexsha == head_before


@pytest.mark.asyncio
async def test_commit_during_merge_creates_merge_commit_and_clears_merge_state(
    member, seeded
):
    repo, repo_path = seeded()
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    with pytest.raises(git.GitCommandError):
        repo.git.merge("theirs")
    _resolve_readme_conflict(repo, repo_path)

    result = await member.run(lambda s: s.commit(repo_path, "merge theirs"))

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert [parent.hexsha for parent in created.parents] == [ours, theirs]
    assert created.author.email == "aiko@example.com"
    assert created.committer.email == "aiko@example.com"
    _assert_no_in_progress_git_state(repo)


@pytest.mark.asyncio
async def test_commit_during_merge_with_ours_tree_creates_merge_commit(member, seeded):
    repo, repo_path = seeded()
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    with pytest.raises(git.GitCommandError):
        repo.git.merge("theirs")
    (repo_path / "README.md").write_text("ours\n", encoding="utf-8")
    repo.git.add("README.md")
    assert not repo.index.diff(repo.head.commit)

    result = await member.run(lambda s: s.commit(repo_path, "keep ours"))

    assert result.status == "committed"
    created = repo.commit(result.commit_sha)
    assert [parent.hexsha for parent in created.parents] == [ours, theirs]
    assert created.tree.hexsha == repo.commit(ours).tree.hexsha
    _assert_no_in_progress_git_state(repo)


@pytest.mark.asyncio
async def test_commit_succeeds_when_repository_requires_gpg_sign(member, seeded):
    repo, repo_path = seeded()
    with repo.config_writer() as writer:
        writer.set_value("commit", "gpgsign", "true")
    (repo_path / "README.md").write_text("initial\nsigned-config\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await member.run(lambda s: s.commit(repo_path, "unsigned member commit"))

    created = repo.commit(result.commit_sha)
    assert created.author.email == "aiko@example.com"
    assert not created.gpgsig


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cherry-pick", "revert"])
async def test_commit_consumes_sequencer_head_after_conflict_resolution(
    member, seeded, operation
):
    repo, repo_path = seeded()
    ours, theirs = _diverged_readme_branches(repo, repo_path)
    with pytest.raises(git.GitCommandError):
        if operation == "cherry-pick":
            repo.git.cherry_pick(theirs)
        else:
            repo.git.revert(theirs)
    _resolve_readme_conflict(repo, repo_path)

    result = await member.run(lambda s: s.commit(repo_path, f"finish {operation}"))

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
async def test_a_failing_hook_runs_in_the_environment_and_is_reported(
    member, seeded, tmp_path
):
    """The project's hooks run inside the environment, and what they print
    comes back to the agent when they refuse the commit."""
    repo, repo_path = seeded()
    ran = tmp_path / "hook-ran"
    _hook(
        repo_path,
        "pre-commit",
        f'echo "$GUILDBOTICS_TEST_GUEST" > "{ran.as_posix()}"; echo lint failed; exit 1',
    )
    member.stage()

    with pytest.raises(MemberCapabilityError, match="lint failed"):
        await member.run(lambda s: s.commit(repo_path, "refused"))

    assert ran.read_text(encoding="utf-8").strip() == "1"


@pytest.mark.asyncio
async def test_publish_rejects_empty_commit_message(member, seeded):
    _, repo_path = seeded()

    with pytest.raises(MemberCapabilityError, match="must not be empty"):
        await member.run(lambda s: s.publish(repo_path, "  \n"))


@pytest.mark.asyncio
async def test_publish_rejects_repo_outside_member_workspace(member, tmp_path):
    outside = tmp_path / "outside"
    git.Repo.init(outside)

    with pytest.raises(MemberCapabilityError, match="member workspace"):
        await member.run(lambda s: s.publish(outside, "message"))


# -- push -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_commits_pushes_and_preserves_worktree(member, host_git):
    await member.prepare()
    readme = member.clone / "README.md"
    untracked = member.clone / "new.txt"
    untracked.write_text("new\n", encoding="utf-8")
    # Staging is a plain git step the caller performs; the member capability
    # only commits what is already staged.
    repo = member.stage(content="initial\nchanged\n")

    result = await member.run(lambda s: s.publish(member.clone, "publish changes"))

    assert result.has_changes is True
    assert result.pushed is True
    assert result.branch == "ticket/1"
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish changes", "url": ""}
    ]
    assert readme.read_text(encoding="utf-8") == "initial\nchanged\n"
    assert untracked.read_text(encoding="utf-8") == "new\n"
    assert repo.is_dirty(untracked_files=True) is False
    assert member.remote().commit("ticket/1").hexsha == result.commit_sha
    published = repo.commit(result.commit_sha)
    assert published.author.email == "aiko@example.com"
    assert published.committer.email == "aiko@example.com"
    # The clone knows it is pushed.
    assert repo.commit("refs/remotes/origin/ticket/1").hexsha == result.commit_sha
    push = next(call for call in host_git if "push" in call.args)
    assert push.args[-3:] == (
        "--",
        member.url("owner"),
        "refs/heads/ticket/1:refs/heads/ticket/1",
    )


@pytest.mark.asyncio
async def test_push_without_local_commits_reports_up_to_date(member):
    await member.prepare(repo="owner/repo", branch="main")

    result = await member.run(lambda s: s.push(member.clone))

    assert result.pushed is False
    assert result.status == "up_to_date"
    assert result.commits == []
    assert result.pull_requests == []
    assert result.pull_requests_error is None


@pytest.mark.asyncio
async def test_push_includes_readiness_for_open_prs_on_the_branch(member, monkeypatch):
    await member.prepare()
    member.repo().index.commit("local commit")
    calls = {}

    async def fake_open_pr_checks(remote_url, branch):
        calls.update({"remote_url": remote_url, "branch": branch})
        return [
            {"pr_url": "https://github.com/owner/repo/pull/7", "readiness": "blocked"}
        ]

    monkeypatch.setattr(member.service.github, "open_pr_checks", fake_open_pr_checks)

    result = await member.run(lambda s: s.push(member.clone))

    assert calls == {"remote_url": member.url("owner"), "branch": "ticket/1"}
    assert result.pull_requests[0]["readiness"] == "blocked"
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
    member, monkeypatch, failure
):
    await member.prepare()
    commit_sha = member.stage().index.commit("local commit").hexsha

    async def failed_open_pr_checks(_remote_url, _branch):
        raise failure

    monkeypatch.setattr(member.service.github, "open_pr_checks", failed_open_pr_checks)

    result = await member.run(lambda s: s.push(member.clone))

    assert result.pushed is True
    assert result.commits[0]["id"] == commit_sha
    assert result.pull_requests_error == str(failure)
    assert member.remote().commit("ticket/1").hexsha == commit_sha


@pytest.mark.asyncio
async def test_push_rejected_by_remote_raises(member, tmp_path):
    await member.prepare(repo="owner/repo", branch="main")
    remote_sha = _advance(member.remotes["owner"], tmp_path, "remote\n")
    local_sha = member.stage().index.commit("local commit").hexsha

    with pytest.raises(MemberCapabilityError) as excinfo:
        await member.run(lambda s: s.push(member.clone))

    message = str(excinfo.value)
    assert "Failed to push 'main' to origin" in message
    assert "non-fast-forward" in message
    assert member.remote().commit("main").hexsha == remote_sha != local_sha


@pytest.mark.asyncio
async def test_push_to_an_unreachable_remote_raises(member, tmp_path):
    await member.prepare()
    member.stage().index.commit("local commit")
    member.remotes["owner"].rename(tmp_path / "gone.git")

    with pytest.raises(MemberCapabilityError, match="git fetch failed"):
        await member.run(lambda s: s.push(member.clone))


@pytest.mark.asyncio
async def test_a_clone_prepare_did_not_check_out_is_not_pushed(member, seeded):
    _, repo_path = seeded()
    member.stage().index.commit("local commit")

    with pytest.raises(MemberCapabilityError, match="member git prepare"):
        await member.run(lambda s: s.push(repo_path))


@pytest.mark.asyncio
async def test_a_bundle_over_the_limit_is_refused(member, monkeypatch):
    await member.prepare()
    member.stage("big.bin", os.urandom(4096).hex()).index.commit("big")
    monkeypatch.setattr(member_git, "MAX_GIT_BUNDLE_BYTES", 1024)

    with pytest.raises(MemberCapabilityError, match="too much output"):
        await member.run(lambda s: s.push(member.clone))

    assert "ticket/1" not in [head.name for head in member.remote().heads]


@pytest.mark.asyncio
async def test_chat_run_cannot_push_unchecked_work(member):
    from guildbotics.capabilities.chat_updates import (
        ChatUpdatesRequired,
        check_chat_updates,
    )
    from guildbotics.capabilities.task_runs import RunStore
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus

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
    await member.prepare()
    commit_sha = member.stage().index.commit("local commit").hexsha
    invocation = MemberInvocation(run_id="chat-run", guest=member.guest)
    with member_invocation_scope(invocation):
        with pytest.raises(ChatUpdatesRequired):
            await member.service.push(member.clone)
        assert "ticket/1" not in [head.name for head in member.remote().heads]
        check_chat_updates("aiko", "chat-run")
        await member.service.push(member.clone)
    assert member.remote().commit("ticket/1").hexsha == commit_sha


# -- prepare --------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_checks_out_a_new_ticket_branch_from_the_default(member):
    result = await member.prepare()

    repo = member.repo()
    assert result == {
        "repo": "owner/repo",
        "checkout_repo": "owner/repo",
        "repo_path": str(member.clone),
        "branch": "ticket/1",
        "default_branch": "main",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "pr_url": "",
        "mode": "issue",
    }
    assert repo.active_branch.name == "ticket/1"
    assert repo.head.commit == member.remote().commit("main")
    assert repo.remote("origin").url == member.url("owner")
    with repo.config_reader() as config:
        assert config.get_value("user", "email") == "aiko@example.com"


@pytest.mark.asyncio
async def test_prepare_pull_request_review_checks_out_the_fork_head(member, tmp_path):
    fork = git.Repo(member.remotes["contributor"])
    fork.git.branch("feature", "main")
    pr_url = "https://github.com/owner/repo/pull/7"
    member.heads[pr_url] = GitHubPullRequestHead("contributor", "repo", "feature")

    result = await member.prepare(
        issue_url="https://github.com/owner/repo/issues/42", pr_url=pr_url
    )

    assert result["repo"] == "owner/repo"
    assert result["checkout_repo"] == "contributor/repo"
    assert result["branch"] == "feature"
    assert result["mode"] == "pull_request_review"
    repo = member.repo()
    assert repo.active_branch.name == "feature"
    assert repo.remote("origin").url == member.url("contributor")
    assert repo.active_branch.tracking_branch().path == "refs/remotes/origin/feature"


@pytest.mark.asyncio
async def test_prepare_branch_mode_checks_out_ad_hoc_branch(member):
    # Chat-originated work has no issue: prepare must work from just a
    # repository and a branch name.
    result = await member.prepare(repo="owner/repo", branch="chat/fix-typo")

    assert result["repo"] == "owner/repo"
    assert result["branch"] == "chat/fix-typo"
    assert result["mode"] == "branch"
    assert result["issue_url"] == result["pr_url"] == ""
    assert member.repo().active_branch.name == "chat/fix-typo"


@pytest.mark.asyncio
async def test_prepare_follows_the_remote_and_drops_what_is_not_committed(
    member, tmp_path
):
    """What is not committed is dropped; commits the remote lacks survive a
    branch it can fast-forward, and a diverged branch is reset to it."""
    await member.prepare(repo="owner/repo", branch="main")
    local_sha = member.stage().index.commit("unpushed").hexsha
    (member.clone / "untracked.txt").write_text("x", encoding="utf-8")
    (member.clone / "README.md").write_text("dirty\n", encoding="utf-8")

    await member.prepare(repo="owner/repo", branch="main")

    repo = member.repo()
    assert repo.head.commit.hexsha == local_sha
    assert not (member.clone / "untracked.txt").exists()
    assert not repo.is_dirty()

    remote_sha = _advance(member.remotes["owner"], tmp_path, "remote\n")
    await member.prepare(repo="owner/repo", branch="main")

    assert member.repo().head.commit.hexsha == remote_sha
    assert member.repo().commit("refs/remotes/origin/main").hexsha == remote_sha


@pytest.mark.asyncio
async def test_prepare_requires_an_anchor(member):
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await member.run(lambda s: s.prepare())
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await member.run(lambda s: s.prepare(repo="acme/widget"))


@pytest.mark.asyncio
async def test_prepare_rejects_malformed_repo(member):
    with pytest.raises(MemberCapabilityError, match="<owner>/<repo>"):
        await member.run(lambda s: s.prepare(repo="acme", branch="chat/fix-typo"))
    with pytest.raises(MemberCapabilityError, match="Invalid repository"):
        await member.run(lambda s: s.prepare(repo="acme/..", branch="chat/fix-typo"))


@pytest.mark.asyncio
async def test_a_fork_and_its_upstream_push_where_each_was_prepared(member, tmp_path):
    """Both are the same clone directory; the last ``prepare`` decides where
    it pushes."""
    fork = git.Repo(member.remotes["contributor"])
    fork.git.branch("feature", "main")
    pr_url = "https://github.com/owner/repo/pull/7"
    member.heads[pr_url] = GitHubPullRequestHead("contributor", "repo", "feature")

    await member.prepare(pr_url=pr_url)
    feature = member.stage(content="fork\n").index.commit("to the fork").hexsha
    await member.run(lambda s: s.push(member.clone))
    await member.prepare()
    ticket = member.stage(content="upstream\n").index.commit("upstream").hexsha
    await member.run(lambda s: s.push(member.clone))

    assert member.remote("contributor").commit("feature").hexsha == feature
    assert member.remote("owner").commit("ticket/1").hexsha == ticket
    assert "ticket/1" not in [head.name for head in member.remote("contributor").heads]
    assert "feature" not in [head.name for head in member.remote("owner").heads]


@pytest.mark.asyncio
async def test_a_worktree_and_a_submodule_of_a_clone_commit_and_push(
    member, tmp_path, worker_git_seed
):
    await member.prepare()
    repo = member.repo()
    submodule = tmp_path / "library"
    worker_git_seed.copy(worker_git_seed.member_worktree, submodule)
    repo.git.execute(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule),
            "lib",
        ]
    )
    superproject = await member.run(lambda s: s.publish(member.clone, "add library"))
    worktree = member.service.workspace_root / "wt"
    repo.git.worktree("add", "-b", "feature", str(worktree))
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    git.Repo(worktree).git.add(A=True)

    added = await member.run(lambda s: s.publish(worktree, "feature in a worktree"))

    assert member.remote().commit("ticket/1").hexsha == superproject.commit_sha
    assert member.remote().commit("feature").hexsha == added.commit_sha


# -- the host never follows the clone ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "commit", "push", "publish"])
async def test_member_mode_git_is_refused_outside_a_command(member, operation):
    """Without the command's environment there is nowhere to run a clone's git."""
    calls = {
        "prepare": lambda s: s.prepare(repo="owner/repo", branch="main"),
        "commit": lambda s: s.commit(member.clone, "message"),
        "push": lambda s: s.push(member.clone),
        "publish": lambda s: s.publish(member.clone, "message"),
    }

    with pytest.raises(MemberCapabilityError, match="isolated environment"):
        await calls[operation](member.service)


def test_member_git_starts_processes_only_through_subprocess_run() -> None:
    """The population ``host_git`` records: the host starts no git of member
    git's any other way (GitPython included), and the token helpers start
    none."""
    package = Path(member_git.__file__).parent.parent
    for relative, allowed in (
        ("capabilities/member_git.py", {"run", "TimeoutExpired"}),
        ("utils/git_tool.py", set()),
    ):
        tree = ast.parse((package / relative).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imported & {"git", "asyncio", "multiprocessing", "pty"}, relative
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"subprocess", "os"}
            and node.attr.startswith(
                (
                    "run",
                    "Popen",
                    "call",
                    "check",
                    "system",
                    "exec",
                    "spawn",
                    "popen",
                    "Timeout",
                )
            )
        }
        assert used == allowed, relative


def _hook(repo_path: Path, name: str, script: str) -> None:
    hook = Path(git.Repo(repo_path).git_dir) / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    hook.chmod(0o755)


def _trap(path: Path, marker: Path) -> str:
    """A command that leaves ``marker`` only when it runs on the host."""
    return (
        f'[ -n "$GUILDBOTICS_TEST_GUEST" ] || touch "{(marker / path.name).as_posix()}"'
    )


def _plant_hooks(member: _Member, trap: Path, marker: Path) -> None:
    for name in (
        "pre-commit",
        "pre-push",
        "post-checkout",
        "post-merge",
        "reference-transaction",
        "pre-auto-gc",
    ):
        _hook(member.clone, name, _trap(Path(name), marker))


def _plant_fsmonitor(member: _Member, trap: Path, marker: Path) -> None:
    script = member.clone / "fsmonitor.sh"
    script.write_text(
        f"#!/bin/sh\n{_trap(Path('fsmonitor'), marker)}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    member.repo().git.config("core.fsmonitor", script.as_posix())


def _plant_filter(member: _Member, trap: Path, marker: Path) -> None:
    repo = member.repo()
    for kind in ("smudge", "clean"):
        repo.git.config(
            f"filter.evil.{kind}", f"sh -c '{_trap(Path(kind), marker)}; cat'"
        )
    (member.clone / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")


def _plant_remote(member: _Member, trap: Path, marker: Path) -> None:
    attacker = trap.as_uri()
    repo = member.repo()
    repo.git.remote("set-url", "origin", attacker)
    repo.git.config(f"url.{attacker}.insteadOf", member.url("owner"))
    repo.git.config("http.proxy", "http://attacker.invalid:8080")


def _plant_gitfile(member: _Member, trap: Path, marker: Path) -> None:
    dot_git = member.clone / ".git"

    def writable(function, path, _):
        os.chmod(path, 0o700)
        function(path)

    shutil.rmtree(dot_git, onexc=writable)
    dot_git.write_text(f"gitdir: {(trap / '.git').as_posix()}\n", encoding="utf-8")


def _plant_worktree(member: _Member, trap: Path, marker: Path) -> None:
    member.repo().git.config("core.worktree", trap.as_posix())


def _plant_symlink(member: _Member, trap: Path, marker: Path) -> None:
    moved = member.clone.with_name("moved")
    member.clone.rename(moved)
    member.clone.symlink_to(trap, target_is_directory=True)


_PLANTS = {
    "hooks": _plant_hooks,
    "fsmonitor": _plant_fsmonitor,
    "filter": _plant_filter,
    "remote": _plant_remote,
    "gitfile": _plant_gitfile,
    "core.worktree": _plant_worktree,
    "symlink": _plant_symlink,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("plant", sorted(_PLANTS))
async def test_what_a_clone_plants_never_runs_on_the_host_or_gets_the_token(
    member, host_git, tmp_path, plant, request
):
    """A turn rewrites the clone, then asks for every member git operation.

    Whatever the rewrite points at -- hooks, a filter, an fsmonitor, another
    remote, another repository by gitfile, ``core.worktree`` or symlink -- the
    host runs none of it, and the member's token goes only where the host
    derived it should.
    """
    if plant == "symlink":
        request.getfixturevalue("symlinks")
    marker = tmp_path / "ran-on-host"
    marker.mkdir()
    # Another repository of the user's, with hooks of its own.
    trap = tmp_path / "users-repository"
    git.Repo.init(trap)
    for name in ("pre-commit", "post-checkout", "pre-push", "reference-transaction"):
        _hook(trap, name, _trap(Path(f"trap-{name}"), marker))
    await member.prepare()
    # Closed before planting: on Windows, the processes an open repository
    # keeps hold its directory, which the symlink plant moves.
    with member.stage(content="before\n") as repo:
        repo.index.commit("before")
    _PLANTS[plant](member, trap, marker)
    host_git.clear()

    # ``prepare`` last: it rewrites the clone's remote.
    for operation in (
        lambda s: s.commit(member.clone, "planted"),
        lambda s: s.push(member.clone),
        lambda s: s.publish(member.clone, "planted"),
        lambda s: s.prepare(issue_url="https://github.com/owner/repo/issues/1"),
    ):
        try:
            await member.run(operation)
        except MemberCapabilityError:
            pass

    assert sorted(path.name for path in marker.iterdir()) == []
    clones = member.service.workspace_root
    for call in host_git:
        assert not call.cwd.is_relative_to(clones)
        assert not call.cwd.is_relative_to(trap)
        assert not any(
            str(where) in arg or where.as_posix() in arg
            for arg in call.args
            for where in (clones, trap)
        )
        if call.env.get("GIT_PASSWORD"):
            assert call.cwd == member.origin / "owner" / "repo.git"
            assert member.url("owner") in call.args
    assert any(call.env.get("GIT_PASSWORD") for call in host_git)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["--upload-pack=x", "-x", "a..b", "a:b", "HEAD"])
async def test_a_branch_name_git_would_not_take_as_one_is_refused(
    member, host_git, branch
):
    with pytest.raises(MemberCapabilityError, match="branch"):
        await member.run(lambda s: s.prepare(repo="owner/repo", branch=branch))

    await member.prepare()
    member.stage().index.commit("local")
    (member.clone / ".git" / "HEAD").write_text(
        f"ref: refs/heads/{branch}\n", encoding="utf-8"
    )
    host_git.clear()

    for operation in (
        lambda s: s.push(member.clone),
        lambda s: s.commit(member.clone, "message"),
    ):
        with pytest.raises(MemberCapabilityError, match="branch"):
            await member.run(operation)

    assert all(
        call.args[1:3] == ("check-ref-format", "--branch")
        for call in host_git
        if any(branch in arg for arg in call.args)
    )
    assert [head.name for head in member.remote().heads] == ["main"]


class _TaggingGuest(_LocalGuest):
    """An environment whose clone slips a tag of its own into what it sends."""

    def run(self, argv, **options):
        if tuple(argv[:4]) == ("git", "bundle", "create", "-q"):
            options["stdin"] = options["stdin"] + b"refs/tags/planted\n"
        return super().run(argv, **options)


@pytest.mark.asyncio
async def test_the_host_takes_only_the_branch_of_what_a_clone_sends(
    member, host_git, monkeypatch
):
    """Not a tag the clone named beside it, even for a user whose git follows
    tags on push; and what it takes in is checked every time."""
    await member.prepare()
    repo = member.stage()
    repo.index.commit("local")
    repo.create_tag("planted", message="planted")
    member.guest = _TaggingGuest()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "push.followTags")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    result = await member.run(lambda s: s.push(member.clone))

    assert result.pushed is True
    origin = git.Repo(member.origin / "owner" / "repo.git")
    assert "planted" not in [tag.name for tag in origin.tags]
    assert "planted" not in [tag.name for tag in member.remote().tags]
    [taken] = [
        call
        for call in host_git
        if "fetch" in call.args and "-q" in call.args and "--no-tags" in call.args
    ]
    assert taken.args[1:3] == ("-c", "fetch.fsckObjects=true")


@pytest.mark.asyncio
async def test_a_prepare_that_failed_leaves_where_the_clone_pushes(member, tmp_path):
    await member.prepare()
    member.remotes["contributor"].rename(tmp_path / "gone.git")
    pr_url = "https://github.com/owner/repo/pull/7"
    member.heads[pr_url] = GitHubPullRequestHead("contributor", "repo", "feature")
    with pytest.raises(MemberCapabilityError, match="git fetch failed"):
        await member.prepare(pr_url=pr_url)
    sha = member.stage().index.commit("to the upstream").hexsha

    await member.run(lambda s: s.push(member.clone))

    assert member.remote().commit("ticket/1").hexsha == sha


@pytest.mark.asyncio
async def test_the_environment_is_given_nothing_but_git_identity(member):
    await member.prepare()
    member.stage().index.commit("local")
    await member.run(lambda s: s.publish(member.clone, "message"))

    for argv, _, env in member.guest.runs:
        assert set(env) <= _GUEST_ENV_KEYS, argv
        assert _TOKEN not in " ".join([*argv, *env.values()])


# -- the user's own repository ------------------------------------------------


@pytest.mark.asyncio
async def test_publish_current_workspace_runs_the_users_hooks_on_the_host(
    member, tmp_path, worker_git_seed
):
    repo_path = tmp_path / "current" / "repo"
    worker_git_seed.copy(worker_git_seed.member_worktree, repo_path)
    repo = git.Repo(repo_path)
    repo.remote("origin").set_url(str(member.remotes["owner"]))
    ran = tmp_path / "hook-ran"
    _hook(
        repo_path,
        "pre-commit",
        f'echo "host$GUILDBOTICS_TEST_GUEST" > "{ran.as_posix()}"',
    )
    (repo_path / "README.md").write_text("initial\ncurrent\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await member.service.publish_current_workspace(
        repo_path, "publish current workspace", cwd=repo_path
    )

    assert result.pushed is True
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish current workspace", "url": ""}
    ]
    assert repo.active_branch.name == "main"
    assert member.remote().commit("main").hexsha == result.commit_sha
    assert repo.commit(result.commit_sha).author.email == "aiko@example.com"
    assert ran.read_text(encoding="utf-8").strip() == "host"


@pytest.mark.asyncio
async def test_publish_current_workspace_rejects_repo_that_is_not_current_workspace(
    member, tmp_path, worker_git_seed
):
    current = tmp_path / "current"
    worker_git_seed.copy(worker_git_seed.member_worktree, current)
    other = tmp_path / "other"
    git.Repo.init(other)

    with pytest.raises(MemberCapabilityError, match="current workspace repository"):
        await member.service.publish_current_workspace(other, "message", cwd=current)
