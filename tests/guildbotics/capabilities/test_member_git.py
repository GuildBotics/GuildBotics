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
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import git
import pytest

from guildbotics.capabilities import member_git
from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.entities.team import Person
from guildbotics.integrations import repository_scope
from guildbotics.integrations.local import store
from guildbotics.integrations.local.code_hosting import LocalCodeHostingService
from guildbotics.integrations.local.tickets import LocalTicketService
from guildbotics.runtime.code_hosting_service import GitCredential
from guildbotics.runtime.member_invocation import (
    ChatSubject,
    GuestProcessError,
    GuestResult,
    MemberInvocation,
    Work,
    member_invocation_scope,
)
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from tests.git_seed import WorkerGitSeed
from tests.guildbotics.client_git import (
    current as client_current,
    tracking as client_tracking,
)
from guildbotics.cli import member_client
from tests.guildbotics.local_code_host import (
    REPOSITORY,
    TRACKER,
    local_team,
    pull_request,
    ticket,
    tracker,
)

GUEST_MARK = "GUILDBOTICS_TEST_GUEST"
#: A repository of the same name as ``REPOSITORY``, outside the scope the
#: member writes in.
FORK = "contributor/team/demo"
#: The remotes a member reaches, by who keeps them.
_REPOSITORIES = {"owner": REPOSITORY, "contributor": FORK}
#: The ticket of ``REPOSITORY`` the member works on, and its branch.
_ISSUE = f"local://tickets/{TRACKER}/T-1"
_TICKET_BRANCH = "ticket/T-1"
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
_USERNAME = "provider-user"


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

    def __init__(self, sh: str) -> None:
        self._sh = sh
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
        program = self._sh if argv[0] == "sh" else argv[0]
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


class _CredentialedHost(LocalCodeHostingService):
    """The local code host with a credential, which git must be given only
    toward it."""

    async def git_credential(self) -> GitCredential:
        return GitCredential(username=_USERNAME, password=_TOKEN)


def test_auth_environment_appends_credential_reset_to_command_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.sslVerify")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")

    with member_git._git_auth_environment(
        GitCredential(username=_USERNAME, password=_TOKEN)
    ) as env:
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_1"] == ""


@pytest.mark.asyncio
@pytest.mark.windows
async def test_member_auth_environment_passes_provider_credentials_and_resets_helpers(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    repo_path = tmp_path / "repo"
    helper_marker = tmp_path / "external-helper-ran"
    with git.Repo.init(repo_path) as repo, repo.config_writer() as writer:
        writer.set_value(
            "credential",
            "helper",
            f'!printf invoked > "{helper_marker.as_posix()}"',
        )
    person = _person()
    credential = await _CredentialedHost(person, local_team(person)).git_credential()

    with member_git._git_auth_environment(credential) as auth:
        askpass_path = Path(auth["GIT_ASKPASS"])
        script = askpass_path.read_text(encoding="utf-8")
        assert credential.username not in script
        assert credential.password not in script
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=repo_path,
            env={**os.environ, **auth},
            input="protocol=https\nhost=example.invalid\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0
    assert f"username={credential.username}" in result.stdout
    assert f"password={credential.password}" in result.stdout
    assert not helper_marker.exists()
    assert not askpass_path.parent.exists()


def test_auth_environment_removes_askpass_after_failure():
    with pytest.raises(RuntimeError, match="git failed"):
        with member_git._git_auth_environment(
            GitCredential(username=_USERNAME, password=_TOKEN)
        ) as env:
            askpass_path = Path(env["GIT_ASKPASS"])
            assert askpass_path.exists()
            raise RuntimeError("git failed")

    assert not askpass_path.parent.exists()


@pytest.mark.asyncio
async def test_local_git_needs_no_authentication():
    person = _person()
    credential = await LocalCodeHostingService(
        person, local_team(person)
    ).git_credential()

    assert credential is None
    with member_git._git_auth_environment(credential) as env:
        assert "GIT_ASKPASS" not in env
        # The user's own helper, scoped to the code host, is not asked either.
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""


@dataclass
class _Member:
    """A member with a command's environment, and the remotes it reaches.

    ``remotes[owner]`` is the repository ``_REPOSITORIES[owner]`` of the local
    code host.
    """

    service: MemberGitWorkspaceService
    guest: _LocalGuest
    remotes: dict[str, Path]
    work: Work = field(default_factory=lambda: Work.of_ticket(_ISSUE))
    paths: dict[str, Path] = field(default_factory=dict)

    def pull_request(self, number: int, head: str, head_repository: str = "") -> str:
        """Open ``!<number>`` of ``REPOSITORY`` from ``head`` (of
        ``head_repository``)."""
        return pull_request(number, head, head_repository=head_repository or REPOSITORY)

    def clone_of(self, repository: str) -> Path:
        """The last checkout prepared here, or the seeded ticket's checkout."""
        return self.paths.get(repository, self.service.workspace_root / "seeded")

    @property
    def clone(self) -> Path:
        return self.clone_of(REPOSITORY)

    def url(self, owner: str) -> str:
        return self.remotes[owner].as_uri()

    def origin(self, repository: str = REPOSITORY) -> Path:
        """The host's own repository of ``repository``."""
        *namespace, name = repository.split("/")
        return self.service.host_root.joinpath(
            "repositories", *namespace, f"{name}.git"
        )

    def run(self, operation: Callable[[MemberGitWorkspaceService], object]):
        async def invoke():
            with member_invocation_scope(
                MemberInvocation(guest=self.guest, work=self.work)
            ):
                return await operation(self.service)  # type: ignore[misc]

        return invoke()

    async def prepare(self, **anchor: str) -> dict:
        anchor = anchor or {"issue_url": _ISSUE}
        result = await self.run(lambda service: service.prepare(**anchor))
        self.paths[result["repository"]] = Path(result["repo_path"])
        return result

    def repo(self, repository: str = REPOSITORY) -> git.Repo:
        return git.Repo(self.clone_of(repository))

    def remote(self, owner: str = "owner") -> git.Repo:
        return git.Repo(self.remotes[owner])

    def stage(
        self,
        name: str = "README.md",
        content: str = "changed\n",
        repository: str = REPOSITORY,
    ) -> git.Repo:
        (self.clone_of(repository) / name).write_text(content, encoding="utf-8")
        repo = self.repo(repository)
        repo.git.add(A=True)
        return repo


@pytest.fixture
def member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_git_seed: WorkerGitSeed,
    posix_sh: str,
) -> _Member:
    remotes: dict[str, Path] = {}
    for owner, repository in _REPOSITORIES.items():
        remotes[owner] = store.bare(repository)
        worker_git_seed.copy(worker_git_seed.member_remote, remotes[owner])
    ticket(1)
    person = _person()
    team = local_team(person)
    code = _CredentialedHost(person, team)
    tickets = LocalTicketService(logging.getLogger(__name__), person, team)
    service = MemberGitWorkspaceService(person, team, lambda: code, lambda: tickets)
    # The command's turns work in it: it is there before any of them runs.
    service.workspace_root.mkdir(parents=True)
    return _Member(service, _LocalGuest(posix_sh), remotes)


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
async def test_show_reads_fresh_default_branch_without_guest_or_checkout(
    member: _Member, tmp_path, host_git, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("reading requires neither a guest nor permission to write")

    monkeypatch.setattr(member.service, "_guest", forbidden)
    monkeypatch.setattr(member_git, "ensure_chat_current", forbidden)
    first = await member.service.show(REPOSITORY, ["README.md", "absent", "."])
    assert first["default_branch"] == "main"
    assert first["items"][0]["content"] == "initial\n"
    assert first["items"][1] == {"path": "absent", "kind": "missing"}
    assert first["items"][2]["entries"] == [{"name": "README.md", "kind": "file"}]
    sha = _advance(member.remotes["owner"], tmp_path, "new")
    second = await member.service.show(REPOSITORY, ["README.md"])
    assert second["commit"] == sha != first["commit"]
    assert second["items"][0]["content"] == "new"
    assert not list(member.service.workspace_root.iterdir())
    calls = [c for c in host_git if "fetch" in c.args]
    assert len(calls) == 2
    assert all(c.cwd == member.origin() for c in calls)
    assert all(c.env["GIT_CONFIG_VALUE_0"] == "" for c in calls)
    assert member.guest.runs == []


@pytest.mark.asyncio
async def test_show_uses_changed_default_branch_and_ignores_checkout(
    member: _Member, tmp_path
):
    await member.prepare()
    with member.stage(content="local changes"):
        pass
    first = await member.service.show(REPOSITORY, ["README.md"])
    with member.remote() as remote:
        remote.git.branch("release/next", "main")
        remote.git.symbolic_ref("HEAD", "refs/heads/release/next")
    result = await member.service.show(REPOSITORY, ["README.md"])
    assert result["default_branch"] == "release/next"
    assert result["commit"] == first["commit"]
    assert result["items"][0]["content"] == "initial\n"
    assert (member.clone / "README.md").read_text() == "local changes"


@pytest.mark.asyncio
async def test_show_failed_fetch_does_not_report_missing(member, monkeypatch):
    def failed(*args):
        raise MemberCapabilityError("fetch failed")

    monkeypatch.setattr(member_git._Origin, "fetch", failed)
    with pytest.raises(MemberCapabilityError, match="fetch failed"):
        await member.service.show(REPOSITORY, ["absent"])


@pytest.mark.asyncio
async def test_show_respects_invocation_deadline(member):
    with member_invocation_scope(MemberInvocation(deadline=0)):
        with pytest.raises(MemberCapabilityError):
            await member.service.show(REPOSITORY, ["."])


@pytest.mark.asyncio
async def test_show_literal_paths_binary_limits_and_link_kinds(
    member: _Member, tmp_path
):
    source = tmp_path / "source"
    with git.Repo.clone_from(member.remotes["owner"], source) as repo:
        (source / "dir").mkdir()
        for name, data in {
            "dir/規約.md": "規約\n".encode(),
            "literal*": b"literal",
            "literal-other": b"other",
            "-option": b"dash",
            "empty": b"",
            "nul": b"text\0data",
            "invalid": b"\xff",
            "large": b"x" * (member_git.MAX_GIT_SHOW_BYTES + 1),
            "boundary": b"x" * member_git.MAX_GIT_SHOW_BYTES,
        }.items():
            (source / name).write_bytes(data)
        repo.git.add(A=True)
        # Write links through the index so this works on Windows without symlink rights.
        target = tmp_path / "link-target"
        target.write_bytes(b"/outside")
        link = repo.git.hash_object("-w", str(target))
        repo.git.update_index("--add", "--cacheinfo", f"120000,{link},link")
        repo.git.update_index(
            "--add", "--cacheinfo", f"160000,{repo.head.commit.hexsha},module"
        )
        sha = repo.index.commit(
            "objects", author=git.Actor("Test", "test@example.com")
        ).hexsha
        repo.git.push("origin", "main")
    paths = [
        "dir",
        "dir/規約.md",
        "literal*",
        "-option",
        "empty",
        "nul",
        "invalid",
        "large",
        "boundary",
        "link",
        "module",
    ]
    result = await member.service.show(REPOSITORY, paths)
    items = {item["path"]: item for item in result["items"]}
    assert result["commit"] == sha
    assert items["dir"]["entries"] == [{"name": "規約.md", "kind": "file"}]
    assert items["dir/規約.md"]["content"] == "規約\n"
    assert items["literal*"]["content"] == "literal"
    assert items["-option"]["content"] == "dash"
    assert items["empty"]["content"] == ""
    for name in ("nul", "invalid"):
        assert items[name]["status"] == "binary" and "content" not in items[name]
    assert items["large"]["status"] == "too_large"
    assert "content" not in items["large"]
    assert len(items["boundary"]["content"]) == member_git.MAX_GIT_SHOW_BYTES
    assert (items["link"]["kind"], items["link"]["content"]) == ("symlink", "/outside")
    assert items["module"]["kind"] == "submodule"
    repeated = await member.service.show(REPOSITORY, ["boundary"] * 20)
    assert len(json.dumps(repeated).encode()) < member_git.MAX_PAGE_BYTES
    assert any(i["status"] == "too_large" for i in repeated["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../outside", "dir/../outside", "a\\b", "a\0b"]
)
async def test_show_refuses_paths_outside_the_repository(member, path, host_git):
    with pytest.raises(MemberCapabilityError):
        await member.service.show(REPOSITORY, [path])
    assert host_git == []


@pytest.mark.asyncio
async def test_show_refuses_unbounded_requests(member, host_git):
    for paths in ([], ["."] * member_git.MAX_PAGE_BYTES):
        with pytest.raises(MemberCapabilityError):
            await member.service.show(REPOSITORY, paths)
    assert host_git == []


@pytest.mark.asyncio
async def test_show_omits_large_trees_and_blobs_before_reading_contents(
    member, monkeypatch, host_git
):
    monkeypatch.setattr(member_git, "MAX_GIT_SHOW_BYTES", 1)
    result = await member.service.show(REPOSITORY, [".", "README.md"])
    assert [i["status"] for i in result["items"]] == ["too_large", "too_large"]
    assert all("entries" not in i and "content" not in i for i in result["items"])
    assert not any("blob" in call.args for call in host_git)


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["x", "規", "\x01", "\U0001f30f"])
async def test_show_bounds_serialized_content_with_escaping_and_many_paths(
    member, tmp_path, character
):
    source = tmp_path / "source"
    content = character * (member_git.MAX_GIT_SHOW_BYTES // len(character.encode()))
    with git.Repo.clone_from(member.remotes["owner"], source) as repo:
        (source / "content").write_text(content, encoding="utf-8")
        repo.git.add("content")
        repo.index.commit("content", author=git.Actor("Test", "test@example.com"))
        repo.git.push("origin", "main")
    # This many paths fit the request metadata budget but cannot all return content.
    result = await member.service.show(REPOSITORY, ["content"] * 1000)
    assert len(result["items"]) == 1000
    assert len(json.dumps(result).encode()) < member_git.MAX_PAGE_BYTES
    assert (
        len(json.dumps(result, ensure_ascii=False).encode()) < member_git.MAX_PAGE_BYTES
    )
    for item in result["items"]:
        if item["status"] == "ok":
            assert item["content"] == content
        else:
            assert item["status"] == "too_large" and "content" not in item
    assert result["items"][-1]["status"] == "too_large"
    if character == "x":
        assert result["items"][0]["content"] == content


@pytest.mark.asyncio
async def test_show_keeps_metadata_only_items_within_reserved_budget(
    member, monkeypatch
):
    # SHA-256 gitlinks have the largest metadata, and have no payload to omit.
    def show_path(_origin, _commit, path):
        if path == "s":
            return {"path": path, "kind": "submodule", "commit": "f" * 64}
        content = "x" * (member_git.MAX_GIT_SHOW_BYTES if path == "a" else 163000)
        return {
            "path": path,
            "kind": "file",
            "size": len(content),
            "status": "ok",
            "content": content,
        }

    monkeypatch.setattr(member_git._Origin, "show_path", show_path)
    result = await member.service.show(REPOSITORY, ["a", "b"] + ["s"] * 1229)
    assert result["items"][0]["status"] == result["items"][1]["status"] == "ok"
    assert len(json.dumps(result).encode()) < member_git.MAX_PAGE_BYTES
    assert all("status" not in item for item in result["items"][2:])


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
    assert result.branch == _TICKET_BRANCH
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish changes", "url": ""}
    ]
    assert readme.read_text(encoding="utf-8") == "initial\nchanged\n"
    assert untracked.read_text(encoding="utf-8") == "new\n"
    assert repo.is_dirty(untracked_files=True) is False
    assert member.remote().commit(_TICKET_BRANCH).hexsha == result.commit_sha
    published = repo.commit(result.commit_sha)
    assert published.author.email == "aiko@example.com"
    assert published.committer.email == "aiko@example.com"
    # The clone knows it is pushed.
    assert (
        repo.commit(f"refs/remotes/origin/{_TICKET_BRANCH}").hexsha == result.commit_sha
    )
    push = next(call for call in host_git if "push" in call.args)
    assert push.args[-2:] == (
        "origin",
        f"refs/heads/{_TICKET_BRANCH}:refs/heads/{_TICKET_BRANCH}",
    )
    _connects_to(push, member.url("owner"))


@pytest.mark.asyncio
async def test_push_without_local_commits_reports_up_to_date(member):
    await member.prepare(repo=REPOSITORY, branch="main")

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

    pr_url = member.pull_request(7, _TICKET_BRANCH)
    open_pull_requests = member.service.code.open_pull_requests

    async def observed(repository, branch):
        calls.update({"repository": repository, "branch": branch})
        return await open_pull_requests(repository, branch)

    monkeypatch.setattr(member.service.code, "open_pull_requests", observed)

    result = await member.run(lambda s: s.push(member.clone))

    assert calls == {"repository": REPOSITORY, "branch": _TICKET_BRANCH}
    assert result.pull_requests == [
        {
            "url": pr_url,
            "readiness": "ready",
            "completion_blockers": [],
        }
    ]
    assert "pull_requests_error" not in result.to_dict()


@pytest.mark.asyncio
async def test_push_stays_successful_when_pr_readiness_lookup_fails(
    member, monkeypatch
):
    await member.prepare()
    commit_sha = member.stage().index.commit("local commit").hexsha
    failure = ServiceError("The code host is unavailable.")

    async def failed_open_pr_checks(_repository, _branch):
        raise failure

    monkeypatch.setattr(
        member.service.code, "open_pull_requests", failed_open_pr_checks
    )

    result = await member.run(lambda s: s.push(member.clone))

    assert result.pushed is True
    assert result.commits[0]["id"] == commit_sha
    assert result.pull_requests_error == str(failure)
    assert member.remote().commit(_TICKET_BRANCH).hexsha == commit_sha


@pytest.mark.asyncio
async def test_push_rejected_by_remote_raises(member, tmp_path):
    await member.prepare(repo=REPOSITORY, branch="main")
    remote_sha = _advance(member.remotes["owner"], tmp_path, "remote\n")
    local_sha = member.stage().index.commit("local commit").hexsha

    with pytest.raises(MemberCapabilityError) as excinfo:
        await member.run(lambda s: s.push(member.clone))

    message = str(excinfo.value)
    assert "Failed to push 'main' to origin" in message
    assert "non-fast-forward" in message
    assert member.remote().commit("main").hexsha == remote_sha != local_sha


@pytest.mark.asyncio
async def test_push_to_a_remote_that_is_gone_raises(member, tmp_path):
    await member.prepare()
    member.stage().index.commit("local commit")
    member.remotes["owner"].rename(tmp_path / "gone.git")

    with pytest.raises(ServiceNotFound, match="has no remote repository"):
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

    assert _TICKET_BRANCH not in [head.name for head in member.remote().heads]


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
        {"event_ids": ["E1"]},
    )
    ChatReceiveStatus().save("slack", "aiko", "C1", state="ready")
    member.work = Work.of_chat(ChatSubject("slack", "C1", "100.1", "E1", "U_BOT"))
    await member.prepare()
    commit_sha = member.stage().index.commit("local commit").hexsha
    invocation = MemberInvocation(
        run_id="chat-run",
        work=Work.of_chat(ChatSubject("slack", "C1", "100.1", "E1", "U_BOT")),
        guest=member.guest,
    )
    with member_invocation_scope(invocation):
        with pytest.raises(ChatUpdatesRequired):
            await member.service.push(member.clone)
        assert _TICKET_BRANCH not in [head.name for head in member.remote().heads]
        check_chat_updates("aiko")
        await member.service.push(member.clone)
    assert member.remote().commit(_TICKET_BRANCH).hexsha == commit_sha


# -- prepare --------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_checks_out_a_new_ticket_branch_from_the_default(member):
    result = await member.prepare()

    repo = member.repo()
    assert result == {
        "repository": REPOSITORY,
        "repo_path": str(member.clone),
        "branch": _TICKET_BRANCH,
        "default_branch": "main",
        "issue_url": _ISSUE,
        "pr_url": "",
        "mode": "issue",
    }
    assert repo.active_branch.name == _TICKET_BRANCH
    assert repo.head.commit == member.remote().commit("main")
    assert repo.remote("origin").url == member.url("owner")
    with repo.config_reader() as config:
        assert config.get_value("user", "email") == "aiko@example.com"


@pytest.mark.asyncio
async def test_prepare_pull_request_review_checks_out_the_fork_head(member, tmp_path):
    fork = git.Repo(member.remotes["contributor"])
    fork.git.branch("feature", "main")
    pr_url = member.pull_request(7, "feature", FORK)

    result = await member.prepare(issue_url=_ISSUE, pr_url=pr_url)

    assert result["repository"] == FORK
    assert result["repo_path"] == str(member.clone_of(FORK))
    assert result["branch"] == "feature"
    assert result["mode"] == "pull_request_review"
    repo = member.repo(FORK)
    assert repo.active_branch.name == "feature"
    assert repo.remote("origin").url == member.url("contributor")
    assert repo.active_branch.tracking_branch().path == "refs/remotes/origin/feature"


@pytest.mark.asyncio
async def test_prepare_branch_mode_checks_out_ad_hoc_branch(member):
    # Chat-originated work has no ticket: prepare must work from just a
    # repository and a branch name.
    result = await member.prepare(repo=REPOSITORY, branch="chat/fix-typo")

    assert result["repository"] == REPOSITORY
    assert result["branch"] == "chat/fix-typo"
    assert result["mode"] == "branch"
    assert result["issue_url"] == result["pr_url"] == ""
    assert member.repo().active_branch.name == "chat/fix-typo"


@pytest.mark.asyncio
async def test_prepare_preserves_uncommitted_work_on_the_same_branch(member, tmp_path):
    """A continued run retains its HEAD, index, tracked and untracked edits."""
    await member.prepare(repo=REPOSITORY, branch="main")
    local_sha = member.stage().index.commit("unpushed").hexsha
    (member.clone / "staged.txt").write_text("staged\n", encoding="utf-8")
    member.repo().git.add("staged.txt")
    index_tree = member.repo().index.write_tree().hexsha
    (member.clone / "untracked.txt").write_text("x", encoding="utf-8")
    (member.clone / "README.md").write_text("dirty\n", encoding="utf-8")

    await member.prepare(repo=REPOSITORY, branch="main")

    repo = member.repo()
    assert repo.head.commit.hexsha == local_sha
    assert (member.clone / "untracked.txt").read_text() == "x"
    assert (member.clone / "README.md").read_text() == "dirty\n"
    assert repo.is_dirty()
    assert repo.index.write_tree().hexsha == index_tree

    remote_sha = _advance(member.remotes["owner"], tmp_path, "remote\n")
    await member.prepare(repo=REPOSITORY, branch="main")

    assert member.repo().head.commit.hexsha == local_sha
    assert member.repo().commit("refs/remotes/origin/main").hexsha == remote_sha


@pytest.mark.asyncio
async def test_prepare_updates_a_clean_checkout_to_the_new_remote_head(
    member, tmp_path
):
    await member.prepare(repo=REPOSITORY, branch="main")
    remote_sha = _advance(member.remotes["owner"], tmp_path, "new head\n")
    await member.prepare(repo=REPOSITORY, branch="main")
    assert member.repo().head.commit.hexsha == remote_sha


@pytest.mark.asyncio
@pytest.mark.windows
@pytest.mark.parametrize("same_work", [True, False])
@pytest.mark.parametrize("remote_change", ["rewrite", "delete"])
async def test_idle_checkout_follows_remote_changes(
    member, tmp_path, same_work, remote_change
):
    other_path = tmp_path / "rewriter"
    other = git.Repo.clone_from(str(member.remotes["owner"]), other_path, branch="main")
    with other.config_writer() as writer:
        writer.set_value("user", "name", "Other")
        writer.set_value("user", "email", "other@example.com")
    other.git.checkout("-b", "feature")
    _commit_readme(other, other_path, "before rewrite\n", "feature")
    other.git.push("origin", "feature")
    first = await member.prepare(repo=REPOSITORY, branch="feature")
    cache = _warm_cache(member.repo())
    member.repo().git.config("branch.autoSetupMerge", "false")
    if remote_change == "rewrite":
        (other_path / "README.md").write_text("rewritten\n", encoding="utf-8")
        other.git.add("README.md")
        other.git.commit("--amend", "-m", "rewritten")
        other.git.push("--force", "origin", "feature")
        expected = other.head.commit.hexsha
    else:
        other.git.push("origin", "--delete", "feature")
        expected = _advance(member.remotes["owner"], tmp_path, "new default\n")
    if not same_work:
        member.work = Work("manual", "next-work")

    prepared = await member.prepare(repo=REPOSITORY, branch="feature")

    assert prepared["repo_path"] == first["repo_path"]
    assert prepared["branch"] == "feature"
    assert member.repo().head.commit.hexsha == expected
    tracked = "feature" if remote_change == "rewrite" else "main"
    assert (
        member.repo().active_branch.tracking_branch().path
        == f"refs/remotes/origin/{tracked}"
    )
    assert cache.read_text() == "warm"
    assert _clone_count(member) == 1


def _warm_cache(repo: git.Repo, value: str = "warm") -> Path:
    (Path(repo.git_dir) / "info" / "exclude").write_text("target/\n", encoding="utf-8")
    cache = Path(repo.working_tree_dir) / "target" / "cache"
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(value, encoding="utf-8")
    return cache


def _remove_tree(path: Path) -> None:
    def writable(function, name, _):
        os.chmod(name, 0o700)
        function(name)

    shutil.rmtree(path, onexc=writable)


def _clone_count(member) -> int:
    return sum(
        path.is_dir() and len(path.name) == 64
        for path in member.service.workspace_root.iterdir()
    )


@pytest.mark.asyncio
@pytest.mark.windows
async def test_clean_checkouts_are_reused_with_their_build_cache(member):
    first = await member.prepare(repo=REPOSITORY, branch="main")
    cache = _warm_cache(member.repo())
    for kind in ("ticket", "chat", "manual", "ticket", "chat", "manual"):
        member.work = Work(kind, f"new-{kind}-{member.work.identity}")
        prepared = await member.prepare(repo=REPOSITORY, branch="next")
        assert prepared["repo_path"] == first["repo_path"]
        assert cache.read_text() == "warm"
        assert _clone_count(member) == 1


@pytest.mark.asyncio
@pytest.mark.windows
async def test_idle_clones_are_pruned_after_parallel_work_finishes(member):
    paths = []
    for number in range(4):
        member.work = Work("ticket", f"work-{number}")
        prepared = await member.prepare(repo=REPOSITORY, branch=f"work/{number}")
        path = Path(prepared["repo_path"])
        _warm_cache(member.repo(), str(number))
        (path / "pending.txt").write_text("unfinished", encoding="utf-8")
        paths.append(path)
    assert _clone_count(member) == 4
    for path in paths:
        (path / "pending.txt").unlink()
    member.work = Work("manual", "next-run")
    prepared = await member.prepare(repo=REPOSITORY, branch="main")
    assert prepared["repo_path"] in {str(path) for path in paths}
    assert _clone_count(member) == 1
    assert (Path(prepared["repo_path"]) / "target" / "cache").exists()


@pytest.mark.asyncio
async def test_a_published_clone_can_be_reused_after_squash_and_branch_deletion(
    member, tmp_path
):
    first = await member.prepare()
    cache = _warm_cache(member.repo())
    member.stage().index.commit("feature work")
    await member.run(lambda service: service.push(member.clone))
    _advance(member.remotes["owner"], tmp_path, "squashed work\n")
    member.remote().git.branch("-D", _TICKET_BRANCH)
    member.work = Work("ticket", "next-ticket")
    prepared = await member.prepare(repo=REPOSITORY, branch="main")
    assert prepared["repo_path"] == first["repo_path"]
    assert cache.read_text() == "warm"
    assert _clone_count(member) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recreated", [False, True])
async def test_reassigned_same_branch_starts_at_the_current_remote_or_default(
    member, tmp_path, recreated
):
    first = await member.prepare()
    member.stage().index.commit("old feature")
    await member.run(lambda service: service.push(member.clone))
    expected = _advance(member.remotes["owner"], tmp_path, "squashed feature\n")
    member.remote().git.branch("-D", _TICKET_BRANCH)
    if recreated:
        member.remote().git.branch(_TICKET_BRANCH, "main")
    place = "acme/team/second-tickets"
    tracker(place, repository=REPOSITORY)
    url = ticket(1, place=place)
    member.work = Work.of_ticket(url)
    prepared = await member.prepare(issue_url=url)
    assert prepared["repo_path"] == first["repo_path"]
    assert member.repo().head.commit.hexsha == expected


@pytest.mark.asyncio
async def test_remote_tracking_refs_are_not_unpublished_local_work(member):
    first = await member.prepare(repo=REPOSITORY, branch="main")
    baseline = member.repo().head.commit.hexsha
    dangling = member.stage().index.commit("discarded local work").hexsha
    member.repo().git.reset("--hard", baseline)
    member.repo().git.update_ref("refs/remotes/other/old-feature", dangling)
    member.work = Work("manual", "next-run")
    prepared = await member.prepare(repo=REPOSITORY, branch="next")
    assert prepared["repo_path"] == first["repo_path"]


@pytest.mark.asyncio
async def test_fetched_tag_history_remains_public_after_remote_tag_deletion(member):
    remote = member.remote()
    remote.git.execute(
        [
            "git",
            "-c",
            "user.name=Remote",
            "-c",
            "user.email=remote@example.test",
            "tag",
            "-a",
            "release",
            "-m",
            "release",
            "main",
        ]
    )
    first = await member.prepare()
    remote.git.tag("-d", "release")
    member.work = Work("manual", "next-run")
    prepared = await member.prepare(repo=REPOSITORY, branch="main")
    assert prepared["repo_path"] == first["repo_path"]


@pytest.mark.asyncio
@pytest.mark.windows
@pytest.mark.parametrize(
    "state",
    [
        "tracked",
        "assume_unchanged",
        "skip_worktree",
        "staged",
        "untracked",
        "unpublished",
        "other_branch",
        "forged_remote",
        "stash",
        "tag",
        "detached",
        "worktree",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "MERGE_AUTOSTASH",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "BISECT_LOG",
        "index.lock",
        "HEAD.lock",
    ],
)
async def test_every_unfinished_git_state_keeps_its_slot(member, state):
    work = member.work
    first = await member.prepare(repo=REPOSITORY, branch="main")
    path = Path(first["repo_path"])
    repo = member.repo()
    baseline = repo.head.commit.hexsha
    if state in {"assume_unchanged", "skip_worktree"}:
        flag = (
            "--assume-unchanged" if state == "assume_unchanged" else "--skip-worktree"
        )
        repo.git.update_index(flag, "README.md")
        (path / "README.md").write_bytes(b"hidden unfinished\x00\xff")
    elif state in {"tracked", "staged", "untracked"}:
        (path / ("extra.txt" if state == "untracked" else "README.md")).write_bytes(
            b"unfinished\x00\xff"
        )
        if state == "staged":
            repo.git.add("README.md")
    elif state in {"unpublished", "other_branch", "forged_remote"}:
        if state == "other_branch":
            repo.git.checkout("-b", "local/unfinished")
        unpublished = member.stage().index.commit("not published").hexsha
        if state == "other_branch":
            repo.git.checkout("main")
        if state == "forged_remote":
            repo.git.update_ref("refs/remotes/origin/main", unpublished)
    elif state == "stash":
        member.stage()
        repo.git.stash("push")
    elif state == "tag":
        repo.git.execute(
            [
                "git",
                "-c",
                "user.name=Local",
                "-c",
                "user.email=local@example.test",
                "tag",
                "-a",
                "private-tag",
                "-m",
                "unfinished",
                "HEAD",
            ]
        )
    elif state == "detached":
        repo.git.checkout("--detach")
    elif state == "worktree":
        repo.git.worktree(
            "add", "-b", "local/worktree", str(member.service.workspace_root / "linked")
        )
    else:
        marker = Path(repo.git_dir) / state
        if state in {"rebase-merge", "rebase-apply", "sequencer"}:
            marker.mkdir()
        else:
            marker.write_text(baseline + "\n", encoding="utf-8")
    head = repo.head.commit.hexsha
    index = {
        key: (entry.mode, entry.hexsha) for key, entry in repo.index.entries.items()
    }
    contents = (path / "README.md").read_bytes()
    local_refs = repo.git.for_each_ref(
        "--format=%(refname) %(objectname)", "refs/heads", "refs/tags", "refs/stash"
    )
    member.work = Work("manual", "other-work")
    second = await member.prepare(repo=REPOSITORY, branch="main")
    assert second["repo_path"] != first["repo_path"]
    assert repo.head.commit.hexsha == head
    assert {
        key: (entry.mode, entry.hexsha) for key, entry in repo.index.entries.items()
    } == index
    assert (path / "README.md").read_bytes() == contents
    assert (
        repo.git.for_each_ref(
            "--format=%(refname) %(objectname)", "refs/heads", "refs/tags", "refs/stash"
        )
        == local_refs
    )
    member.work = work
    resumed = await member.prepare(repo=REPOSITORY, branch="main")
    assert resumed["repo_path"] == first["repo_path"]
    assert repo.head.commit.hexsha == head
    assert resumed["branch"] == ("" if state == "detached" else "main")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit", "push", "publish"])
async def test_member_writes_reject_a_reassigned_checkout(member, operation):
    from guildbotics.utils.i18n_tool import get_language, set_language, t

    owner = member.work
    first = await member.prepare()
    member.work = Work("manual", "other")
    second = await member.prepare()
    assert second["repo_path"] == first["repo_path"]
    member.stage()
    member.work = owner
    call = {
        "commit": lambda s: s.commit(member.clone, "stale"),
        "push": lambda s: s.push(member.clone),
        "publish": lambda s: s.publish(member.clone, "stale"),
    }[operation]
    previous = get_language()
    try:
        for language in ("en", "ja"):
            set_language(language)
            with pytest.raises(MemberCapabilityError) as raised:
                await member.run(call)
            assert str(raised.value) == t("capabilities.member_git.checkout_reassigned")
            assert "capabilities.member_git.checkout_reassigned" not in str(
                raised.value
            )
    finally:
        set_language(previous)
    resumed = await member.prepare()
    assert resumed["repo_path"] != second["repo_path"]


@pytest.mark.asyncio
async def test_failed_initialization_resumes_its_registered_slot(member, monkeypatch):
    run = member.guest.run

    def fail_init(argv, **kwargs):
        if tuple(argv[:2]) == ("git", "init"):
            raise GuestProcessError("initialization stopped")
        return run(argv, **kwargs)

    monkeypatch.setattr(member.guest, "run", fail_init)
    with pytest.raises(MemberCapabilityError, match="initialization stopped"):
        await member.prepare()
    [record] = list((member.service.host_root / "checkouts").glob("*.json"))
    monkeypatch.setattr(member.guest, "run", run)
    resumed = await member.prepare()
    assert Path(resumed["repo_path"]).name == record.stem
    assert _clone_count(member) == 1


@pytest.mark.asyncio
async def test_failed_reassignment_keeps_the_previous_owner(member, monkeypatch):
    first_owner = member.work
    first = await member.prepare()
    run = member.guest.run

    def fail_checkout(argv, **kwargs):
        if tuple(argv[:2]) == ("git", "checkout"):
            raise GuestProcessError("checkout stopped")
        return run(argv, **kwargs)

    monkeypatch.setattr(member.guest, "run", fail_checkout)
    member.work = Work("manual", "other")
    with pytest.raises(MemberCapabilityError, match="checkout stopped"):
        await member.prepare(repo=REPOSITORY, branch="other")
    record = member_git._checkout_record(
        member.service.host_root, Path(first["repo_path"]).name
    )
    assert member_git._Checkout.model_validate_json(
        record.read_text()
    ).owner == member_git._work_id(first_owner)
    monkeypatch.setattr(member.guest, "run", run)
    member.work = first_owner
    assert (await member.prepare())["repo_path"] == first["repo_path"]


@pytest.mark.asyncio
async def test_failed_push_does_not_make_local_history_reusable(member, tmp_path):
    first = await member.prepare(repo=REPOSITORY, branch="main")
    unpublished = member.stage().index.commit("local work").hexsha
    _advance(member.remotes["owner"], tmp_path, "concurrent remote work\n")
    with pytest.raises(MemberCapabilityError, match="Failed to push"):
        await member.run(lambda service: service.push(member.clone))
    member.work = Work("manual", "other-work")
    second = await member.prepare(repo=REPOSITORY, branch="main")
    assert second["repo_path"] != first["repo_path"]
    assert git.Repo(first["repo_path"]).head.commit.hexsha == unpublished


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_idle_cleanup_keeps_its_record_for_the_next_prepare(
    member, monkeypatch, caplog, partial
):
    paths = []
    for number in range(2):
        member.work = Work("manual", f"work-{number}")
        prepared = await member.prepare()
        path = Path(prepared["repo_path"])
        _warm_cache(member.repo())
        (path / "pending.txt").write_text("pending", encoding="utf-8")
        paths.append(path)
    for path in paths:
        (path / "pending.txt").unlink()
    run = member.guest.run

    def fail_delete(argv, **kwargs):
        if argv[0] == "sh" and "rm -rf" in argv[2]:
            git_dir = member.service.workspace_root / argv[-1] / ".git"
            if partial and git_dir.is_dir():
                _remove_tree(git_dir)
            raise GuestProcessError("cleanup stopped")
        return run(argv, **kwargs)

    monkeypatch.setattr(member.guest, "run", fail_delete)
    member.work = Work("manual", "next")
    await member.prepare()
    assert _clone_count(member) == 2
    assert (
        sum(
            len(list((member.service.host_root / name).glob("*.json")))
            for name in ("checkouts", "discarded")
        )
        == 2
    )
    assert "Could not remove idle checkout" in caplog.text
    [discarded] = list((member.service.host_root / "discarded").glob("*.json"))
    if partial:
        held = member.clone
        (held / "pending.txt").write_text("pending", encoding="utf-8")
        member.work = Work("manual", "while-cleanup-is-blocked")
        prepared = await member.prepare()
        assert Path(prepared["repo_path"]).name != discarded.stem
        assert (Path(prepared["repo_path"]) / ".git").is_dir()
        (held / "pending.txt").unlink()
    monkeypatch.setattr(member.guest, "run", run)
    await member.prepare()
    assert _clone_count(member) == 1
    assert len(list((member.service.host_root / "checkouts").glob("*.json"))) == 1
    assert not list((member.service.host_root / "discarded").glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.windows
@pytest.mark.parametrize("other", ["ticket", "same_key", "pr", "branch", "chat"])
async def test_unfinished_work_survives_other_work_in_the_same_repository(
    member, other
):
    """Other anchors and even the same branch have their own HEAD and index."""
    first_work = member.work
    first = await member.prepare()
    first_path = Path(first["repo_path"])
    repo = member.stage(content="committed A\n")
    before = repo.index.commit("local A").hexsha
    member.stage(name="staged.txt", content="staged A\n")
    index = repo.index.write_tree().hexsha
    (first_path / "README.md").write_bytes(b"unstaged A\x00\xff")
    (first_path / "untracked.txt").write_bytes(b"untracked A\x00\xff")

    if other in {"ticket", "same_key"}:
        if other == "same_key":
            place = "acme/team/other-tickets"
            tracker(place, repository=REPOSITORY)
            url = ticket(1, place=place)
        else:
            url = ticket(2)
        member.work = Work.of_ticket(url)
        anchor = {"issue_url": url}
    elif other == "pr":
        member.remote().git.branch(_TICKET_BRANCH, "main")
        url = member.pull_request(7, _TICKET_BRANCH)
        member.work = Work.of_ticket(url)
        anchor = {"pr_url": url}
    else:
        if other == "chat":
            member.work = Work("chat", "local:aiko:C1:T2")
        anchor = {
            "repo": REPOSITORY,
            "branch": _TICKET_BRANCH if other == "chat" else "other",
        }

    second = await member.prepare(**anchor)
    second_path = Path(second["repo_path"])
    assert second_path != first_path
    assert Path(member.repo().git_dir) != Path(repo.git_dir)
    assert member.repo().head.commit.hexsha != before
    assert not (second_path / "staged.txt").exists()
    assert not (second_path / "untracked.txt").exists()
    member.stage(content="B\n")
    await member.run(lambda service: service.publish(second_path, "work B"))

    member.work = first_work
    resumed = await member.prepare()
    assert resumed["repo_path"] == str(first_path)
    assert member.repo().head.commit.hexsha == before
    assert member.repo().index.write_tree().hexsha == index
    assert (first_path / "README.md").read_bytes() == b"unstaged A\x00\xff"
    assert (first_path / "untracked.txt").read_bytes() == b"untracked A\x00\xff"


@pytest.mark.asyncio
@pytest.mark.windows
async def test_preparing_again_keeps_a_dirty_branch_the_agent_created(member):
    first = await member.prepare()
    member.repo().git.checkout("-b", "agent/chosen")
    member.stage(content="unfinished work\n")

    resumed = await member.prepare()

    assert resumed["repo_path"] == first["repo_path"]
    assert resumed["branch"] == "agent/chosen"
    published = await member.run(
        lambda s: s.publish(member.clone, "finish chosen branch")
    )
    assert member.remote().commit("agent/chosen").hexsha == published.commit_sha
    assert _TICKET_BRANCH not in [head.name for head in member.remote().heads]


@pytest.mark.asyncio
async def test_a_ticket_resumed_for_its_prs_checks_reuses_its_checkout(member):
    first = await member.prepare()
    member.remote().git.branch(_TICKET_BRANCH, "main")
    url = member.pull_request(7, _TICKET_BRANCH)
    member.stage(content="unfinished ticket work\n")

    resumed = await member.prepare(pr_url=url)

    assert resumed["repo_path"] == first["repo_path"]
    assert (member.clone / "README.md").read_text() == "unfinished ticket work\n"


def test_slot_and_work_ids_are_bounded_and_keep_distinct_inputs_separate():
    ids = [
        member_git._checkout_id(repository, slot)
        for repository in (REPOSITORY, FORK)
        for slot in range(3)
    ]
    ids += [
        member_git._work_id(Work(kind, identity))
        for kind in ("ticket", "chat", "manual")
        for identity in ("a:b", "a/b", "é", "e\u0301", "x" * 1000)
    ]
    assert len(set(ids)) == len(ids)
    assert all(
        len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
        for value in ids
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_prepare_requires_the_hosts_work_context(member, language):
    from guildbotics.utils.i18n_tool import get_language, set_language, t

    previous = get_language()
    set_language(language)
    try:
        with member_invocation_scope(MemberInvocation(guest=member.guest)):
            with pytest.raises(MemberCapabilityError) as raised:
                await member.service.prepare(issue_url=_ISSUE)
        assert str(raised.value) == t("capabilities.member_git.work_required")
        assert "capabilities.member_git.work_required" not in str(raised.value)
    finally:
        set_language(previous)


@pytest.mark.asyncio
async def test_prepare_keeps_divergent_unpublished_work_on_its_branch(member, tmp_path):
    await member.prepare(repo=REPOSITORY, branch="main")
    local_sha = member.stage().index.commit("local work").hexsha
    _advance(member.remotes["owner"], tmp_path, "remote work\n")
    await member.prepare(repo=REPOSITORY, branch="main")
    assert member.repo().head.commit.hexsha == local_sha


@pytest.mark.asyncio
async def test_preparing_another_branch_keeps_unpushed_default_branch_commits(member):
    first = await member.prepare(repo=REPOSITORY, branch="main")
    local_sha = member.stage().index.commit("unpushed default branch work").hexsha
    await member.prepare(repo=REPOSITORY, branch="another-task")
    assert git.Repo(first["repo_path"]).commit("main").hexsha == local_sha


@pytest.mark.asyncio
async def test_prepare_requires_an_anchor(member):
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await member.run(lambda s: s.prepare())
    with pytest.raises(MemberCapabilityError, match="requires --issue-url"):
        await member.run(lambda s: s.prepare(repo="acme/widget"))


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["acme", "acme/..", "acme//demo"])
async def test_prepare_rejects_malformed_repo(member, repository):
    with pytest.raises(MemberCapabilityError, match="Invalid repository"):
        await member.run(lambda s: s.prepare(repo=repository, branch="chat/fix-typo"))


@pytest.mark.asyncio
async def test_a_ticket_of_no_repository_is_prepared_in_the_one_named(member):
    """A ticket whose tracker belongs to no repository is worked on in the
    repository ``--repo`` names, on the ticket's branch."""
    place = "acme/team/loose"
    tracker(place, repository=None)
    loose = ticket(2, place=place)

    with pytest.raises(MemberCapabilityError, match="belongs to no repository"):
        await member.prepare(issue_url=loose)
    result = await member.prepare(issue_url=loose, repo=REPOSITORY)

    assert (result["repository"], result["branch"]) == (REPOSITORY, "ticket/T-2")
    assert member.repo().active_branch.name == "ticket/T-2"


@pytest.mark.asyncio
async def test_a_repository_other_than_the_tickets_own_is_refused(member):
    with pytest.raises(
        MemberCapabilityError, match=f"belongs to {REPOSITORY}, not {FORK}"
    ):
        await member.prepare(issue_url=_ISSUE, repo=FORK)

    assert not member.clone_of(FORK).exists()


@pytest.mark.asyncio
async def test_repositories_of_one_name_have_a_clone_each(member):
    """``acme/team/demo`` and ``contributor/team/demo`` are both ``demo``:
    each has its own clone, and its own record of where that pushes."""
    own = await member.prepare(repo=REPOSITORY, branch="main")
    fork = await member.prepare(repo=FORK, branch="main")

    assert own["repo_path"] == str(member.clone_of(REPOSITORY))
    assert fork["repo_path"] == str(member.clone_of(FORK))
    assert member.repo(REPOSITORY).remote("origin").url == member.url("owner")
    assert member.repo(FORK).remote("origin").url == member.url("contributor")
    checkouts = member.service.host_root / "checkouts"
    assert sorted(
        member_git._Checkout.model_validate_json(
            record.read_text(encoding="utf-8")
        ).repository
        for record in checkouts.iterdir()
    ) == sorted([REPOSITORY, FORK])


@pytest.mark.asyncio
async def test_a_fork_is_prepared_but_only_its_upstream_is_pushed_to(member, tmp_path):
    """The fork's clone pushes to the fork, which lies outside the scope the
    member writes in: only the configured scope's repository takes a push."""
    fork = git.Repo(member.remotes["contributor"])
    fork.git.branch("feature", "main")
    before = fork.commit("feature").hexsha
    pr_url = member.pull_request(7, "feature", FORK)

    await member.prepare(pr_url=pr_url)
    member.stage(content="fork\n", repository=FORK).index.commit("to the fork")
    with pytest.raises(ServiceError, match=f"Writes are limited to .*refused {FORK}"):
        await member.run(lambda s: s.push(member.clone_of(FORK)))
    await member.prepare()
    ticket = member.stage(content="upstream\n").index.commit("upstream").hexsha
    await member.run(lambda s: s.push(member.clone))

    assert member.remote("contributor").commit("feature").hexsha == before
    assert member.remote("owner").commit(_TICKET_BRANCH).hexsha == ticket
    assert _TICKET_BRANCH not in [
        head.name for head in member.remote("contributor").heads
    ]
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

    assert member.remote().commit(_TICKET_BRANCH).hexsha == superproject.commit_sha
    assert member.remote().commit("feature").hexsha == added.commit_sha


# -- the host never follows the clone ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "commit", "push", "publish"])
async def test_member_mode_git_is_refused_outside_a_command(member, operation):
    """Without the command's environment there is nowhere to run a clone's git."""
    calls = {
        "prepare": lambda s: s.prepare(repo=REPOSITORY, branch="main"),
        "commit": lambda s: s.commit(member.clone, "message"),
        "push": lambda s: s.push(member.clone),
        "publish": lambda s: s.publish(member.clone, "message"),
    }

    with pytest.raises(MemberCapabilityError, match="isolated environment"):
        await calls[operation](member.service)


@pytest.mark.parametrize("module", [member_git, member_git.LocalGit.__module__])
def test_member_git_starts_processes_only_through_subprocess_run(module) -> None:
    """The population ``host_git`` records: the host starts no git of member
    git's any other way (GitPython included)."""
    import importlib

    if isinstance(module, str):
        module = importlib.import_module(module)
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"git", "asyncio", "multiprocessing", "pty"}
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
    assert used == (set() if module is member_git else {"run", "TimeoutExpired"})


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
    _remove_tree(dot_git)
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
        lambda s: s.prepare(issue_url=_ISSUE),
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
            assert call.cwd == member.origin()
            _connects_to(call, member.url("owner"))
    assert any(call.env.get("GIT_PASSWORD") for call in host_git)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["--upload-pack=x", "-x", "a..b", "a:b", "HEAD"])
async def test_a_branch_name_git_would_not_take_as_one_is_refused(
    member, host_git, branch
):
    with pytest.raises(MemberCapabilityError, match="branch"):
        await member.run(lambda s: s.prepare(repo=REPOSITORY, branch=branch))

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
    member.guest = _TaggingGuest(member.guest._sh)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "push.followTags")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    result = await member.run(lambda s: s.push(member.clone))

    assert result.pushed is True
    origin = git.Repo(member.origin())
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
    # The fork still names its default branch, but its commits are gone.
    objects = member.remotes["contributor"] / "objects"
    objects.rename(tmp_path / "gone")
    (objects / "pack").mkdir(parents=True)
    pr_url = member.pull_request(7, "feature", FORK)
    with pytest.raises(MemberCapabilityError, match="git fetch failed"):
        await member.prepare(pr_url=pr_url)
    sha = member.stage().index.commit("to the upstream").hexsha

    await member.run(lambda s: s.push(member.clone))

    assert member.remote().commit(_TICKET_BRANCH).hexsha == sha


@pytest.mark.asyncio
async def test_the_environment_is_given_nothing_but_git_identity(member):
    await member.prepare()
    member.stage().index.commit("local")
    await member.run(lambda s: s.publish(member.clone, "message"))

    for argv, _, env in member.guest.runs:
        assert set(env) <= _GUEST_ENV_KEYS, argv
        assert _TOKEN not in " ".join([*argv, *env.values()])


@pytest.mark.asyncio
async def test_git_connects_to_origin_only_after_the_credential_is_asked(
    member, host_git, monkeypatch
):
    """The code host waits in ``git_credential`` until it answers, so each
    operation asks for the credential before any of its git connects."""
    code = member.service.code
    credential = code.git_credential
    #: How many host processes had started at each ask.
    asked: list[int] = []

    async def ask() -> GitCredential:
        asked.append(len(host_git))
        return await credential()

    monkeypatch.setattr(code, "git_credential", ask)

    async def connections(operation: Callable[[], Awaitable[object]]) -> list[str]:
        """What the operation's git does that connects, once it asked."""
        host_git.clear()
        asked.clear()
        await operation()
        runs = [i for i, call in enumerate(host_git) if call.env.get("GIT_PASSWORD")]
        assert len(asked) == 1 and runs and asked[0] <= runs[0]
        return [
            next(arg for arg in host_git[i].args if arg in ("fetch", "push"))
            for i in runs
        ]

    def publish() -> Awaitable[object]:
        member.stage()
        return member.run(lambda s: s.publish(member.clone, "message"))

    assert await connections(member.prepare) == ["fetch"]
    assert await connections(publish) == ["fetch", "push"]


# -- the user's own repository ------------------------------------------------


def _current_repo(
    tmp_path: Path, worker_git_seed: WorkerGitSeed, *config: tuple[str, str]
) -> tuple[git.Repo, Path]:
    """The user's own repository, its ``origin`` configured with ``config``."""
    repo_path = tmp_path / "current" / "repo"
    worker_git_seed.copy(worker_git_seed.member_worktree, repo_path)
    repo = git.Repo(repo_path)
    for key, value in config:
        # The seed's origin already has a URL; everything else is added.
        replace = "--replace-all" if key == "remote.origin.url" else "--add"
        repo.git.config(replace, key, value)
    return repo, repo_path


def _connects_to(call: _HostCall, url: str) -> None:
    """``call`` connects to ``origin`` of its repository, and git resolves
    both of that remote's destinations to ``url`` alone."""
    assert "origin" in call.args, call
    repo = git.Repo(call.cwd)
    for direction in ((), ("--push",)):
        listed = repo.git.remote("get-url", *direction, "--all", "origin")
        assert listed.splitlines() == [url]


def _credentialed(host_git: list[_HostCall]) -> list[_HostCall]:
    return [call for call in host_git if call.env.get("GIT_PASSWORD") == _TOKEN]


def _only_the_hosts_repositories_get_the_token(
    member: _Member, host_git: list[_HostCall]
) -> None:
    """Every git given the member's credential runs in a repository of the
    host's own, never in one whose configuration the host did not write."""
    runs = _credentialed(host_git)
    assert runs
    for call in runs:
        assert member.service.host_root in call.cwd.parents, call
        assert call.env["GIT_USERNAME"] == _USERNAME


@pytest.mark.asyncio
@pytest.mark.windows
async def test_publish_current_workspace_commits_with_the_users_hooks_and_pushes_from_the_host(
    member, tmp_path, worker_git_seed, host_git
):
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    ran = tmp_path / "hook-ran"
    _hook(
        repo_path,
        "pre-commit",
        f'echo "host$GUILDBOTICS_TEST_GUEST" > "{ran.as_posix()}"',
    )
    # The push leaves from the host's own repository, not from this one.
    pushed_from_here = tmp_path / "pre-push-ran"
    _hook(repo_path, "pre-push", f'touch "{pushed_from_here.as_posix()}"')
    (repo_path / "README.md").write_text("initial\ncurrent\n", encoding="utf-8")
    repo.git.add(A=True)

    result = await client_current(
        member, repo_path, message="publish current workspace", cwd=repo_path
    )

    assert result.pushed is True
    assert result.commits == [
        {"id": result.commit_sha, "message": "publish current workspace", "url": ""}
    ]
    assert repo.active_branch.name == "main"
    assert member.remote().commit("main").hexsha == result.commit_sha
    assert repo.commit("refs/remotes/origin/main").hexsha == result.commit_sha
    assert repo.commit(result.commit_sha).author.email == "aiko@example.com"
    assert ran.read_text(encoding="utf-8").strip() == "host"
    assert not pushed_from_here.exists()
    _only_the_hosts_repositories_get_the_token(member, host_git)


@pytest.mark.asyncio
async def test_push_current_workspace_goes_where_git_would_push(
    member, tmp_path, worker_git_seed, host_git
):
    """``pushurl`` decides which repository is pushed to, as git would; the
    host then pushes there itself."""
    repo, repo_path = _current_repo(
        tmp_path,
        worker_git_seed,
        ("remote.origin.url", member.url("contributor")),
        ("remote.origin.pushurl", member.url("owner")),
    )
    contributor = member.remote("contributor").commit("main").hexsha
    sha = repo.index.commit("local").hexsha

    result = await client_current(member, repo_path, cwd=repo_path)

    assert result.pushed is True
    assert member.remote("owner").commit("main").hexsha == sha
    assert member.remote("contributor").commit("main").hexsha == contributor
    _only_the_hosts_repositories_get_the_token(member, host_git)


@pytest.mark.asyncio
async def test_push_current_workspace_publishes_a_branch_new_to_the_remote(
    member, tmp_path, worker_git_seed, host_git
):
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    repo.git.switch("-c", "feature/new")
    sha = repo.index.commit("new work").hexsha

    result = await client_current(member, repo_path, cwd=repo_path)

    assert result.pushed is True
    assert result.branch == "feature/new"
    assert member.remote().commit("feature/new").hexsha == sha
    assert repo.commit("refs/remotes/origin/feature/new").hexsha == sha
    _only_the_hosts_repositories_get_the_token(member, host_git)


@pytest.mark.asyncio
async def test_push_current_workspace_behind_the_remote_is_rejected_with_origin_fetched(
    member, tmp_path, worker_git_seed
):
    """As ``git fetch`` then ``git push`` did: the push is rejected, and the
    user's ``origin/<branch>`` shows what the remote has."""
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    remote = member.remote()
    tip = remote.commit("main")
    identity = {
        f"GIT_{role}_{field}": value
        for role in ("AUTHOR", "COMMITTER")
        for field, value in (("NAME", "Someone"), ("EMAIL", "someone@example.com"))
    }
    with remote.git.custom_environment(**identity):
        ahead = remote.git.commit_tree(tip.tree.hexsha, "-p", tip.hexsha, m="remote")
    remote.git.update_ref("refs/heads/main", ahead)
    repo.index.commit("local")

    with pytest.raises(MemberCapabilityError, match="Failed to push 'main'"):
        await client_current(member, repo_path, cwd=repo_path)

    assert remote.commit("main").hexsha == ahead
    assert repo.commit("refs/remotes/origin/main").hexsha == ahead


@pytest.mark.asyncio
async def test_push_current_workspace_of_a_branch_without_commits_is_refused(
    member, tmp_path, worker_git_seed
):
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    repo.git.switch("--orphan", "empty")

    with pytest.raises(MemberCapabilityError, match="'empty' has no commit to push"):
        await client_current(member, repo_path, cwd=repo_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["push", "publish", "up_to_date"])
async def test_current_workspace_exchanges_bundles_and_reports_readiness(
    member, tmp_path, worker_git_seed, host_git, operation
):
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    base = repo.head.commit.hexsha
    pr_url = member.pull_request(7, "main")
    remote = member.remote()
    # All fetched remote branches return, but local refs and tags stay local.
    remote.git.update_ref("refs/heads/other", base)
    repo.git.update_ref("refs/remotes/origin/deleted", base)
    repo.create_tag("local-tag")
    remote.create_tag("remote-tag")
    if operation == "publish":
        (repo_path / "README.md").write_text("published\n", encoding="utf-8")
        repo.git.add("README.md")
        result = await client_current(
            member, repo_path, message="bundle publication", cwd=repo_path
        )
    else:
        if operation == "push":
            repo.index.commit("bundle publication")
        result = await client_current(member, repo_path, cwd=repo_path)

    sha = repo.head.commit.hexsha
    assert remote.commit("main").hexsha == sha
    assert repo.commit("origin/main").hexsha == sha
    assert repo.commit("origin/other").hexsha == base
    assert repo.commit("origin/deleted").hexsha == base
    assert [tag.name for tag in repo.tags] == ["local-tag"]
    assert [tag.name for tag in remote.tags] == ["remote-tag"]
    assert result.pushed is (operation != "up_to_date")
    assert result.pull_requests == [
        {"url": pr_url, "readiness": "ready", "completion_blockers": []}
    ]
    # Neither side fetches the other's repository path.
    fetches = [call for call in host_git if "fetch" in call.args]
    assert fetches
    for call in fetches:
        assert call.cwd != repo_path
        if "--" in call.args:
            assert call.args[call.args.index("--") + 1].endswith(".bundle")
    # Every returned tip is already local, so only the refs need updating.
    assert not any(
        call.cwd == repo_path and "unbundle" in call.args for call in host_git
    )
    if operation != "up_to_date":
        assert any(
            call.cwd == repo_path and "create" in call.args and "bundle" in call.args
            for call in host_git
        )
    _only_the_hosts_repositories_get_the_token(member, host_git)


@pytest.mark.asyncio
async def test_current_workspace_retries_a_bundle_with_missing_prerequisites(
    member, tmp_path, worker_git_seed, monkeypatch
):
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    missing = repo.index.commit("prerequisite absent from host").hexsha
    sha = repo.index.commit("tip").hexsha
    create = member_git.LocalGit.branch_bundle
    requests = []
    headers = []

    def with_missing_prerequisite(user, branch, known, bundle):
        requests.append(set(known))
        # Model a prerequisite the host advertised but no longer holds.
        result = create(
            user, branch, [missing] if len(requests) == 1 else known, bundle
        )
        headers.append(bundle.read_bytes().split(b"\n\n", 1)[0])
        return result

    monkeypatch.setattr(member_git.LocalGit, "branch_bundle", with_missing_prerequisite)
    result = await client_current(member, repo_path, cwd=repo_path)

    assert result.pushed
    assert member.remote().commit("main").hexsha == sha
    assert repo.commit("origin/main").hexsha == sha
    assert len(requests) == 2
    assert requests[0]
    assert requests[1] == set()
    assert f"-{missing} ".encode() in headers[0]
    assert not any(line.startswith(b"-") for line in headers[1].splitlines())


def _remote_commit(remote, parent, message):
    identity = {
        f"GIT_{role}_{field}": value
        for role in ("AUTHOR", "COMMITTER")
        for field, value in (("NAME", "Other"), ("EMAIL", "other@example.com"))
    }
    with remote.git.custom_environment(**identity):
        return remote.git.commit_tree(
            remote.commit(parent).tree.hexsha, "-p", parent, m=message
        )


def _return_history(mode, member, origin, path):
    if mode == "current":
        client_tracking(member, origin, path)
    else:
        clone = member_git._GuestGit(member.guest, str(path))
        member_git._send(origin, clone, origin.refs(*member_git._SHARED_REFS))


@pytest.mark.parametrize("mode", ["current", "member"])
def test_returned_bundles_exclude_the_receivers_history(
    member, tmp_path, worker_git_seed, monkeypatch, mode
):
    repo, path = _current_repo(tmp_path, worker_git_seed)
    origin = member_git._Origin(
        tmp_path / "host", REPOSITORY, member.url("owner"), None
    )
    remote = member.remote()
    previous = remote.commit("main").hexsha
    create = member_git.LocalGit.write_bundle
    headers = []

    def observed(sender, bundle, revisions):
        create(sender, bundle, revisions)
        headers.append(bundle.read_bytes().split(b"\n\n", 1)[0])

    monkeypatch.setattr(member_git.LocalGit, "write_bundle", observed)
    for index in range(3):
        tip = _remote_commit(remote, previous, f"remote {index}")
        remote.git.update_ref("refs/heads/main", tip)
        origin.fetch(None)
        _return_history(mode, member, origin, path)
        assert f"-{previous} ".encode() in headers[-1]
        assert repo.commit("origin/main").hexsha == tip
        previous = tip
    assert len(headers) == 3
    # Repeating the return with no new history must not write another pack.
    packs = sorted((Path(repo.git_dir) / "objects" / "pack").glob("*.pack"))
    _return_history(mode, member, origin, path)
    assert len(headers) == 3
    assert sorted((Path(repo.git_dir) / "objects" / "pack").glob("*.pack")) == packs


@pytest.mark.parametrize("mode", ["current", "member"])
def test_shallow_receiver_does_not_advertise_missing_ancestors_as_known(
    member, tmp_path, mode
):
    remote = member.remote()
    base = remote.commit("main").hexsha
    tip = _remote_commit(remote, base, "shallow tip")
    remote.git.update_ref("refs/heads/main", tip)
    remote.git.update_ref("refs/heads/old", base)
    path = tmp_path / "shallow"
    repo = git.Repo.clone_from(member.url("owner"), path, depth=1, branch="main")
    with pytest.raises(git.GitCommandError):
        repo.git.cat_file("-e", base)
    origin = member_git._Origin(
        tmp_path / "host", REPOSITORY, member.url("owner"), None
    )
    origin.fetch(None)

    _return_history(mode, member, origin, path)

    assert repo.commit("origin/old").hexsha == base
    assert repo.commit("origin/main").hexsha == tip


@pytest.mark.asyncio
async def test_current_push_of_an_existing_ancestor_needs_no_bundle(
    member, tmp_path, worker_git_seed
):
    repo, path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    base = repo.head.commit.hexsha
    ahead = repo.index.commit("public descendant").hexsha
    remote = member.remote()
    # Seed the public descendant before the host first fetches it.
    repo.git.push("origin", "main")
    repo.git.switch("-c", "old", base)

    result = await client_current(member, path, cwd=path)

    assert result.pushed
    assert remote.commit("old").hexsha == base
    assert repo.commit("origin/main").hexsha == ahead
    assert repo.commit("origin/old").hexsha == base


@pytest.mark.parametrize("mode", ["current", "member"])
def test_returned_bundle_retries_missing_prerequisites_once(
    member, tmp_path, worker_git_seed, monkeypatch, mode
):
    repo, path = _current_repo(tmp_path, worker_git_seed)
    remote = member.remote()
    missing = _remote_commit(remote, remote.commit("main").hexsha, "missing parent")
    tip = _remote_commit(remote, missing, "remote tip")
    remote.git.update_ref("refs/heads/main", tip)
    origin = member_git._Origin(
        tmp_path / "host", REPOSITORY, member.url("owner"), None
    )
    origin.fetch(None)
    # Model a prerequisite lost after the receiver advertised it.
    monkeypatch.setattr(member_git, "known_commits", lambda git: {missing})
    monkeypatch.setattr(member_client, "known_commits", lambda git: {missing})
    create = member_git.LocalGit.write_bundle
    headers = []

    def observed(sender, bundle, revisions):
        create(sender, bundle, revisions)
        headers.append(bundle.read_bytes().split(b"\n\n", 1)[0])

    monkeypatch.setattr(member_git.LocalGit, "write_bundle", observed)

    _return_history(mode, member, origin, path)

    assert repo.commit("origin/main").hexsha == tip
    assert len(headers) == 2
    assert f"-{missing} ".encode() in headers[0]
    assert not any(line.startswith(b"-") for line in headers[1].splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [True, False])
async def test_tracking_return_failure_preserves_the_push_failure(
    member, tmp_path, worker_git_seed, monkeypatch, caplog, rejected
):
    repo, path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    if rejected:
        _advance(member.remotes["owner"], tmp_path, "diverged remote\n")
    sha = repo.index.commit("local").hexsha

    original = member.service.client_git

    async def fail_return(phase, *args):
        if phase == "tracking":
            raise MemberCapabilityError("tracking lock conflict")
        return await original(phase, *args)

    monkeypatch.setattr(member.service, "client_git", fail_return)
    with pytest.raises(MemberCapabilityError) as error:
        await client_current(member, path, cwd=path)
    if rejected:
        assert "Failed to push 'main'" in str(error.value)
        assert "tracking lock conflict" in caplog.text
        assert member.remote().commit("main").hexsha != sha
    else:
        assert "tracking lock conflict" in str(error.value)
        assert member.remote().commit("main").hexsha == sha


def _in_environment(monkeypatch, tmp_path, settings) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for index, (key, value) in enumerate(settings):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)


def _in_global_config(monkeypatch, tmp_path, settings) -> None:
    lines = []
    for key, value in settings:
        section, rest = key.split(".", 1)
        subsection, name = rest.rsplit(".", 1)
        lines.append(f'[{section} "{subsection}"]\n\t{name} = {value}\n')
    config = tmp_path / "global.gitconfig"
    config.write_text("".join(lines), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def _owner_dir(member: _Member, owner: str) -> str:
    """``file:///.../code/<repository>/``, the directory the repository of
    ``owner`` is kept in."""
    return member.url(owner).rsplit("/", 1)[0] + "/"


@pytest.mark.parametrize(
    "settings",
    [
        lambda m: [
            (f"url.{_owner_dir(m, 'contributor')}.insteadOf", _owner_dir(m, "owner"))
        ],
        lambda m: [
            (
                f"url.{_owner_dir(m, 'contributor')}.pushInsteadOf",
                _owner_dir(m, "owner"),
            )
        ],
        # An empty value is a rule git applies to every URL.
        lambda m: [(f"url.{_owner_dir(m, 'contributor')}.insteadOf", "")],
        lambda m: [(f"url.{_owner_dir(m, 'contributor')}.pushInsteadOf", "")],
        lambda m: [("remote.origin.pushurl", m.url("contributor"))],
        # The fetch moves while the push stays: each destination is checked.
        lambda m: [
            (f"url.{_owner_dir(m, 'contributor')}.insteadOf", _owner_dir(m, "owner")),
            (f"url.{m.url('owner')}.pushInsteadOf", m.url("owner")),
        ],
    ],
    ids=[
        "insteadOf",
        "pushInsteadOf",
        "empty-insteadOf",
        "empty-pushInsteadOf",
        "pushurl",
        "fetch-only",
    ],
)
@pytest.mark.parametrize("source", [_in_environment, _in_global_config])
@pytest.mark.asyncio
async def test_git_configuration_that_moves_origin_is_never_given_the_token(
    member, tmp_path, host_git, monkeypatch, source, settings
):
    """The host's repository connects only to the URL the host derived. What
    the configuration git reads outside that repository (system, global, the
    environment) says of where ``origin`` is would take the credential
    elsewhere, so git's own resolution of it is what is checked."""
    await member.prepare()
    member.stage().index.commit("local")
    contributor = member.remote("contributor").commit("main").hexsha
    source(monkeypatch, tmp_path, settings(member))
    already = len(host_git)

    with pytest.raises(MemberCapabilityError, match="elsewhere"):
        await member.run(lambda s: s.push(member.clone))

    assert _credentialed(host_git[already:]) == []
    assert member.remote("contributor").commit("main").hexsha == contributor


@pytest.mark.asyncio
async def test_a_rewrite_of_another_url_leaves_the_push_alone(
    member, tmp_path, worker_git_seed, host_git, monkeypatch
):
    """Only a rule that applies to the host's URL stops the push; the rest of
    git's configuration stays the user's."""
    repo, repo_path = _current_repo(
        tmp_path, worker_git_seed, ("remote.origin.url", member.url("owner"))
    )
    sha = repo.index.commit("local").hexsha
    _in_environment(
        monkeypatch,
        tmp_path,
        [("url.https://x.example/.insteadOf", "https://elsewhere.example/")],
    )

    result = await client_current(member, repo_path, cwd=repo_path)

    assert result.pushed is True
    assert member.remote().commit("main").hexsha == sha
    _only_the_hosts_repositories_get_the_token(member, host_git)


@pytest.mark.parametrize(
    ("config", "refused"),
    [
        (
            lambda member: [("remote.origin.url", member.url("contributor"))],
            f"refused {FORK}",
        ),
        (
            lambda member: [
                ("remote.origin.url", "https://example.com/owner/repo.git")
            ],
            "does not push to one repository",
        ),
        (
            lambda member: [
                (
                    "remote.origin.url",
                    "https://provider-user:secret@example.com/owner/repo.git",
                )
            ],
            "does not push to one repository",
        ),
        (
            lambda member: [("remote.origin.url", "git@example.com:owner/repo.git")],
            "does not push to one repository",
        ),
        (
            lambda member: [
                ("remote.origin.url", member.url("owner")),
                ("remote.origin.pushurl", member.url("contributor")),
            ],
            f"refused {FORK}",
        ),
        (
            lambda member: [
                ("remote.origin.url", member.url("owner")),
                (f"url.{member.url('contributor')}.pushInsteadOf", member.url("owner")),
            ],
            f"refused {FORK}",
        ),
        (
            lambda member: [
                ("remote.origin.url", member.url("owner")),
                ("remote.origin.pushurl", member.url("owner")),
                ("remote.origin.pushurl", member.url("contributor")),
            ],
            "does not push to one repository",
        ),
    ],
    ids=[
        "another-owner",
        "another-host",
        "credential-in-url",
        "scp-form",
        "pushurl",
        "pushInsteadOf",
        "two-push-urls",
    ],
)
@pytest.mark.asyncio
async def test_push_current_workspace_outside_the_owner_never_reaches_the_remote(
    member, tmp_path, worker_git_seed, host_git, monkeypatch, config, refused
):
    """Where git would push is judged, not ``remote.origin.url``: a repository
    outside the scope, or one that is not the configured code host's, is refused
    before any git is given the member's credential, and the refusal names no
    credential the remote's URL carries."""
    recorded = []
    monkeypatch.setattr(
        repository_scope,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    repo, repo_path = _current_repo(tmp_path, worker_git_seed, *config(member))
    repo.index.commit("local")
    contributor = member.remote("contributor").commit("main").hexsha

    with pytest.raises((ServiceError, MemberCapabilityError), match=refused) as refusal:
        await client_current(member, repo_path, cwd=repo_path)

    assert _credentialed(host_git) == []
    assert not [call for call in host_git if {"fetch", "push"} & set(call.args)]
    assert member.remote("contributor").commit("main").hexsha == contributor
    assert "secret" not in f"{refusal.value} {recorded}"


@pytest.mark.asyncio
async def test_publish_current_workspace_rejects_repo_that_is_not_current_workspace(
    member, tmp_path, worker_git_seed
):
    current = tmp_path / "current"
    worker_git_seed.copy(worker_git_seed.member_worktree, current)
    other = tmp_path / "other"
    git.Repo.init(other)

    with pytest.raises(MemberCapabilityError, match="current workspace repository"):
        await client_current(member, other, message="message", cwd=current)
