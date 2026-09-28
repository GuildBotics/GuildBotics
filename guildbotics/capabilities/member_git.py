"""Commit, push, and prepare the repositories a member works in.

A member's clone (``--workspace-mode member``) is written by the command's
turns, and a repository decides what its git runs: hooks, filters, where a
fetch or a push goes, which directory it works on. Every git that reads or
writes a clone therefore runs inside the command's environment
(:class:`CommandGuest`), never on the host, and outside a command there is
none to run it. The member's credential is only used on the host, against a
repository of the host's own per ``owner/repo`` that no environment mounts,
and toward the URL the host derives. The two exchange history as git bundles:
data, which the host takes only under the branch it checked.

The repository open in the user's own session (``--workspace-mode current``)
is theirs: its commit runs on the host, under their configuration and hooks.
Its push does not: the host takes the branch's history into its own
repository and pushes from there, so the member's credential never goes where
the repository's configuration would send it (and a ``pre-push`` hook does not
run).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import httpx

from guildbotics.capabilities.chat_updates import ensure_chat_current
from guildbotics.capabilities.member_github import (
    MemberCapabilityError,
    MemberGitHubCapabilityService,
)
from guildbotics.entities.team import Person, Team
from guildbotics.integrations.github.github_utils import get_person_github_token
from guildbotics.integrations.github.repository_scope import NAME, check_repository
from guildbotics.runtime.member_invocation import (
    CommandGuest,
    GuestProcessError,
    GuestResult,
    current_member_invocation,
)
from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_local_path
from guildbotics.utils.git_tool import (
    build_git_auth_environment,
    create_git_askpass_script,
)

#: The most a bundle a member's clone sends the host may be; more is refused.
MAX_GIT_BUNDLE_BYTES = 1 << 30
#: The most output of any other git run in a member's clone that is read.
_MAX_GIT_OUTPUT_BYTES = 1 << 24
#: How long a push of the user's own repository waits for the member's git.
_CURRENT_GIT_WAIT_SECONDS = 60.0
#: A full object name, SHA-1 or SHA-256.
_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: What the host fetches of a repository it pushes to.
_FETCH_REFSPECS = ("+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
#: The references a clone is given of the host's repository.
_SHARED_REFS = ("refs/remotes/origin/", "refs/tags/")
#: Store a bundle read from stdin in the clone, without touching its references.
_UNBUNDLE = (
    'f="$(mktemp)" || exit 1; cat >"$f" && git bundle unbundle "$f" >/dev/null;'
    ' s=$?; rm -f "$f"; exit $s'
)
#: Replace the clone's remote-tracking references with those read from stdin.
_SET_REFS = (
    "git for-each-ref --format='delete %(refname)' refs/remotes/origin/"
    " | git update-ref --no-deref --stdin && git update-ref --no-deref --stdin"
)


@dataclass(frozen=True)
class PublishResult:
    repo_path: str
    branch: str
    commit_sha: str | None
    pushed: bool
    has_changes: bool
    status: str
    commits: list[dict[str, str]]
    pull_requests: list[dict[str, Any]]
    pull_requests_error: str | None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "has_changes": self.has_changes,
            "status": self.status,
            "commits": self.commits,
            "pull_requests": self.pull_requests,
        }
        if self.pull_requests_error:
            result["pull_requests_error"] = self.pull_requests_error
        return result


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
    pull_requests: list[dict[str, Any]]
    pull_requests_error: str | None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "pushed": self.pushed,
            "status": self.status,
            "commits": self.commits,
            "pull_requests": self.pull_requests,
        }
        if self.pull_requests_error:
            result["pull_requests_error"] = self.pull_requests_error
        return result


class _Git(Protocol):
    """Where one repository's git runs."""

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        ok: Iterable[int] = (0,),
    ) -> GuestResult: ...


class _HostGit:
    """git run on the host, in a repository the host may trust: the user's
    own, or one of the host's own that nothing else writes."""

    def __init__(
        self,
        cwd: Path,
        timeout: Callable[[], float] | None = None,
        config: tuple[str, ...] = (),
    ) -> None:
        self.cwd = cwd
        self._timeout = timeout
        #: ``-c`` options every git run here starts with.
        self._config = [option for item in config for option in ("-c", item)]

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        ok: Iterable[int] = (0,),
    ) -> GuestResult:
        assert stdout is None, "the host writes its own files with git itself"
        try:
            ran = subprocess.run(
                ["git", *self._config, *args],
                cwd=self.cwd,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
                input=stdin.read_bytes() if isinstance(stdin, Path) else stdin,
                capture_output=True,
                timeout=self._timeout() if self._timeout else None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MemberCapabilityError(f"git {args[0]} ran out of time.") from exc
        except GuestProcessError as exc:
            raise MemberCapabilityError(str(exc)) from exc
        return _checked(args, GuestResult(ran.returncode, ran.stdout, ran.stderr), ok)


class _GuestGit:
    """git run inside the command's environment, in a member's clone."""

    def __init__(self, guest: CommandGuest, cwd: str) -> None:
        self.guest = guest
        self.cwd = cwd

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        ok: Iterable[int] = (0,),
    ) -> GuestResult:
        return self.run(("git", *args), env=env, stdin=stdin, stdout=stdout, ok=ok)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        ok: Iterable[int] = (0,),
    ) -> GuestResult:
        try:
            result = self.guest.run(
                argv,
                cwd=self.cwd,
                env={"GIT_TERMINAL_PROMPT": "0", **(env or {})},
                stdin=stdin,
                stdout=stdout,
                stdout_limit=(
                    MAX_GIT_BUNDLE_BYTES
                    if stdout is not None
                    else _MAX_GIT_OUTPUT_BYTES
                ),
            )
        except GuestProcessError as exc:
            raise MemberCapabilityError(str(exc)) from exc
        return _checked(argv[1:] if argv[0] == "git" else argv, result, ok)


def _checked(
    args: Iterable[str], result: GuestResult, ok: Iterable[int]
) -> GuestResult:
    if result.returncode not in ok:
        command = next(iter(args), "")
        reason = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise MemberCapabilityError(f"git {command} failed: {reason}")
    return result


class _Origin:
    """The host's own repository of one ``owner/repo``, and the URL it
    pushes to. No environment mounts it, and only the host writes it."""

    def __init__(
        self,
        root: Path,
        owner: str,
        repo: str,
        url: str,
        timeout: Callable[[], float] | None,
    ) -> None:
        self.full_repo = f"{owner}/{repo}"
        self.url = url
        path = root / "repositories" / owner / f"{repo}.git"
        if not path.is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
            _HostGit(path.parent, timeout)("init", "--bare", "-q", "--", path.name)
        # Checked on every fetch rather than stored: what it takes in is
        # history a clone sent, and nothing may leave it unchecked.
        self.git = _HostGit(path, timeout, ("fetch.fsckObjects=true",))

    def fetch(self, token: str) -> None:
        with _git_auth_environment(token) as env:
            self.git(
                "fetch", "-q", "--prune", "--", self.url, *_FETCH_REFSPECS, env=env
            )

    def refs(self, *prefixes: str) -> dict[str, str]:
        """Its references under ``prefixes`` (all of them without), by name."""
        return _refs(self.git, *prefixes)

    def has_commit(self, name: str) -> bool:
        return (
            self.git("cat-file", "-e", f"{name}^{{commit}}", ok=(0, 1, 128)).returncode
            == 0
        )

    def head(self, branch: str) -> str:
        return _object_name(
            self.git("rev-parse", "--verify", f"refs/heads/{branch}").stdout
        )


class MemberGitWorkspaceService:
    def __init__(
        self, person: Person, team: Team, logger: logging.Logger | None = None
    ) -> None:
        self.person = person
        self.team = team
        self.logger = logger or logging.getLogger(__name__)
        self.github = MemberGitHubCapabilityService(person, team)
        self.workspace_root = get_member_clone_path(person.person_id)
        #: The host's own repositories and records of the member's clones.
        self.host_root = get_workspace_local_path("member_git", person.person_id)

    async def aclose(self) -> None:
        await self.github.aclose()

    async def prepare(
        self,
        issue_url: str | None = None,
        pr_url: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        # Three anchors decide the checkout: a PR head, an issue-derived
        # ticket/<n> branch, or an ad-hoc repo+branch (e.g. chat-originated work
        # with no issue). Issue anchoring is a ticket-workflow contract, not a
        # capability requirement.
        guest = self._guest()
        anchor_url = pr_url or issue_url
        if anchor_url:
            resource = self.github.parse_url(anchor_url)
            full_repo = resource.full_repo
            if resource.kind == "pull":
                mode = "pull_request_review"
                pr_url = pr_url or anchor_url
                head = await self.github.get_pr_head(pr_url)
                branch = head.branch
                checkout_owner = head.owner
                checkout_repo = head.repo
            else:
                mode = "issue"
                branch = f"ticket/{resource.number}"
                checkout_owner = resource.owner
                checkout_repo = resource.repo
        else:
            if not repo or not branch:
                raise MemberCapabilityError(
                    "prepare requires --issue-url, --pr-url, or --repo with --branch."
                )
            mode = "branch"
            checkout_owner, separator, checkout_repo = repo.partition("/")
            if not separator or not checkout_owner or not checkout_repo:
                raise MemberCapabilityError("--repo must be in <owner>/<repo> format.")
            full_repo = repo
        _name(checkout_owner, "owner")
        _name(checkout_repo, "repository")
        default_branch = await self.github.default_branch(checkout_owner, checkout_repo)
        token = await get_person_github_token(self.person, self.github.base_url)
        with self._locked(self.host_root, guest.remaining()):
            self._branch_name(guest.remaining, branch)
            self._branch_name(guest.remaining, default_branch)
            origin = await self._origin(
                self.host_root, checkout_owner, checkout_repo, guest.remaining
            )
            origin.fetch(token)
            repo_path = self.workspace_root / checkout_repo
            _GuestGit(guest, guest.path(self.workspace_root))(
                "init", "-q", "--", checkout_repo
            )
            clone = _GuestGit(guest, guest.path(repo_path))
            git_user, git_email = self._git_identity()
            for key, value in (
                ("remote.origin.url", origin.url),
                ("remote.origin.fetch", _FETCH_REFSPECS[0]),
                ("user.name", git_user),
                ("user.email", git_email),
            ):
                clone("config", "--replace-all", key, value)
            refs = origin.refs(*_SHARED_REFS)
            _send(origin, clone, refs)
            # Where the clone pushes follows what it now tracks.
            (self.host_root / "checkouts").mkdir(parents=True, exist_ok=True)
            (self.host_root / "checkouts" / checkout_repo).write_text(
                origin.full_repo, encoding="utf-8"
            )
            _check_out(clone, branch, default_branch, refs)
        return {
            "repo": full_repo,
            "checkout_repo": f"{checkout_owner}/{checkout_repo}",
            "repo_path": str(repo_path),
            "branch": branch,
            "default_branch": default_branch,
            "issue_url": issue_url or "",
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
        if not message.strip():
            raise MemberCapabilityError("Commit message must not be empty.")
        if workspace_mode == "current":
            repo_path = self._current_repo(repo_path, cwd or Path.cwd())
            return self._commit(_HostGit(repo_path), repo_path, message, str)
        guest = self._guest()
        repo_path = self._clone_path(repo_path)
        with self._locked(self.host_root, guest.remaining()):
            return self._commit(
                _GuestGit(guest, guest.path(repo_path)),
                repo_path,
                message,
                lambda name: self._branch_name(guest.remaining, name),
            )

    async def push(
        self,
        repo_path: Path,
        *,
        workspace_mode: Literal["member", "current"] = "member",
        cwd: Path | None = None,
    ) -> PushResult:
        """Push the repository's current branch as the member.

        The member's credential is used only in the host's own repository of
        the ``owner/repo`` the push goes to, toward the URL the host derives.
        A repository whose configuration the host did not write -- a member's
        clone, or the user's own -- decides where its git connects and whom
        it hands a credential (``pushurl``, ``pushInsteadOf``,
        ``credential.helper``, ...), so it only supplies the branch's history.
        """
        if workspace_mode == "current":
            repo_path = self._current_repo(repo_path, cwd or Path.cwd())
            source: _Git = _HostGit(repo_path)
            # Repositories of its own, so that the user's push and a
            # command's git never wait for each other.
            root = self.host_root / "current"
            wait, timeout = _CURRENT_GIT_WAIT_SECONDS, None
        else:
            guest = self._guest()
            repo_path = self._clone_path(repo_path)
            source = _GuestGit(guest, guest.path(repo_path))
            root, wait, timeout = self.host_root, guest.remaining(), guest.remaining
        with self._locked(root, wait):
            owner, repo = (
                self._checkout_of(source)
                if isinstance(source, _GuestGit)
                else self._push_destination(source)
            )
            check_repository(self.github.owner, owner, repo)
            branch = self._branch_name(timeout, _current_branch(source))
            origin = await self._origin(root, owner, repo, timeout)
            ensure_chat_current(self.person.person_id)
            token = await get_person_github_token(self.person, self.github.base_url)
            origin.fetch(token)
            sha = (
                _receive(source, origin, branch)
                if isinstance(source, _GuestGit)
                else _exchange(origin, source, repo_path, branch)
            )
            pushed, commits = self._push(origin, branch, token)
            if pushed:
                tracking = f"refs/remotes/origin/{branch}"
                origin.git("update-ref", tracking, sha)
                source("update-ref", tracking, sha)
        pull_requests: list[dict[str, Any]] = []
        pull_requests_error = None
        try:
            pull_requests = await self.github.open_pr_checks(origin.url, branch)
        except (MemberCapabilityError, httpx.HTTPError) as exc:
            pull_requests_error = str(exc)
        return PushResult(
            repo_path=str(repo_path),
            branch=branch,
            pushed=pushed,
            status="pushed" if pushed else "up_to_date",
            commits=commits,
            pull_requests=pull_requests,
            pull_requests_error=pull_requests_error,
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
            pull_requests=push.pull_requests,
            pull_requests_error=push.pull_requests_error,
        )

    def _guest(self) -> CommandGuest:
        """The environment member-mode git runs in.

        Raises:
            MemberCapabilityError: Outside a running command, where none is.
        """
        guest = current_member_invocation().guest
        if guest is None:
            raise MemberCapabilityError(
                "Member workspaces are written by AI CLI turns, so their git runs"
                " only inside the isolated environment of a running GuildBotics"
                " command. Use --workspace-mode current for the repository open"
                " in your own session, or delegate the work with `guildbotics run`."
            )
        return guest

    @contextmanager
    def _locked(self, root: Path, wait: float) -> Iterator[None]:
        """Hold the member's git of the repositories under ``root``: one that
        outlived the broker's wait for it is still running, and the next one
        waits for it to end."""
        try:
            with held_lock(root / "git.lock", timeout=wait):
                yield
        except LockTimeoutError as exc:
            raise MemberCapabilityError(
                "Another git operation of this member is still running."
            ) from exc
        except GuestProcessError as exc:
            raise MemberCapabilityError(str(exc)) from exc

    async def _origin(
        self, root: Path, owner: str, repo: str, timeout: Callable[[], float] | None
    ) -> _Origin:
        url = await self.github.get_clone_url(owner, repo)
        return _Origin(root, owner, repo, url, timeout)

    def _push_destination(self, git: _Git) -> tuple[str, str]:
        """The ``owner/repo`` the user's ``origin`` pushes to, as git resolves
        it (``pushurl``, ``pushInsteadOf``). Only that name is taken from it.
        """
        listed = git("remote", "get-url", "--push", "--all", "origin").stdout
        remotes = listed.decode(errors="replace").splitlines()
        repository = (
            self.github.repository_from_remote(remotes[0])
            if len(remotes) == 1
            else None
        )
        return repository or (
            ", ".join(self.github.remote_host(url) for url in remotes),
            "",
        )

    def _branch_name(self, timeout: Callable[[], float] | None, value: str) -> str:
        """``value`` if git takes it as a branch name as it stands.

        It comes from where the host cannot vouch for it -- the clone, the
        agent, a pull request's author -- and goes into references and
        refspecs, so git itself decides (``HEAD``, ``-x``, ``a..b`` and
        ``a:b`` are not branch names), and it has to be taken as spelled.

        Raises:
            MemberCapabilityError: If it is not a branch name.
        """
        self.host_root.mkdir(parents=True, exist_ok=True)
        checked = _HostGit(self.host_root, timeout)(
            "check-ref-format", "--branch", value, ok=range(256)
        )
        if (
            value.startswith("-")
            or checked.returncode != 0
            or checked.stdout.decode().strip() != value
        ):
            raise MemberCapabilityError(f"Invalid branch name: {value!r}")
        return value

    def _clone_path(self, repo_path: Path) -> Path:
        """``repo_path`` as the host spells it, inside the member's clones.

        Only its spelling is read: what is there is the turns' to write, so
        the host does not follow it.
        """
        root = Path(os.path.abspath(self.workspace_root.expanduser()))
        path = Path(os.path.abspath(repo_path.expanduser()))
        if root not in path.parents:
            raise MemberCapabilityError(
                f"repo_path must be under member workspace root: {root}"
            )
        return path

    def _checkout_of(self, clone: _GuestGit) -> tuple[str, str]:
        """The ``owner/repo`` the clone ``prepare`` last checked out pushes to.

        The clone only names which of the member's clones it belongs to (a
        worktree's is the clone it was added to); where that pushes is the
        host's own record.
        """
        common = PurePosixPath(
            clone("rev-parse", "--path-format=absolute", "--git-common-dir")
            .stdout.decode()
            .strip()
        )
        root = PurePosixPath(clone.guest.path(self.workspace_root))
        record = self.host_root / "checkouts" / common.parent.name
        if (
            common.name != ".git"
            or common.parent.parent != root
            or not NAME.fullmatch(common.parent.name)
            or common.parent.name in {".", ".."}
            or not record.is_file()
        ):
            raise MemberCapabilityError(
                "This repository was not checked out with `member git prepare`;"
                " only a prepared clone can be pushed."
            )
        owner, _, repo = record.read_text(encoding="utf-8").partition("/")
        return owner, repo

    def _current_repo(self, repo_path: Path, cwd: Path) -> Path:
        repo_path = repo_path.expanduser().resolve()
        found = _HostGit(cwd.expanduser().resolve())(
            "rev-parse", "--show-toplevel", ok=(0, 128)
        )
        if found.returncode != 0:
            raise MemberCapabilityError(
                "current workspace mode requires running inside the target git repository."
            )
        current_root = Path(found.stdout.decode().strip()).resolve()
        if repo_path != current_root:
            raise MemberCapabilityError(
                f"repo_path must match the current workspace repository: {current_root}"
            )
        return repo_path

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

    def _commit(
        self,
        git: _Git,
        repo_path: Path,
        message: str,
        branch_name: Callable[[str], str],
    ) -> CommitResult:
        """Commit what is staged, with the member's name.

        ``git commit`` consumes in-progress merge, cherry-pick, and revert
        state. A merge resolved to the current tree has nothing staged and
        still needs its commit. ``--no-gpg-sign`` keeps member commits
        unsigned: the member identity has no signing key, and
        ``commit.gpgsign`` must not start pinentry.
        """
        branch = branch_name(_current_branch(git))
        staged = (
            git("diff", "--cached", "--quiet", ok=(0, 1)).returncode == 1
            or git("rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1)).returncode
            == 0
        )
        commit_sha = None
        if staged:
            git_user, git_email = self._git_identity()
            git(
                "commit",
                "--no-gpg-sign",
                "-F",
                "-",
                env={
                    "GIT_AUTHOR_NAME": git_user,
                    "GIT_AUTHOR_EMAIL": git_email,
                    "GIT_COMMITTER_NAME": git_user,
                    "GIT_COMMITTER_EMAIL": git_email,
                },
                stdin=f"{message.strip()}\n".encode(),
            )
            commit_sha = _object_name(git("rev-parse", "--verify", "HEAD").stdout)
        return CommitResult(
            repo_path=str(repo_path),
            branch=branch,
            commit_sha=commit_sha,
            has_changes=staged,
            status="committed" if commit_sha else "nothing_staged",
        )

    def _push(
        self, origin: _Origin, branch: str, token: str
    ) -> tuple[bool, list[dict[str, str]]]:
        """Push ``branch`` of the host's fetched repository to its URL unless
        it is already there.

        Raises:
            MemberCapabilityError: If the remote did not accept the push.
        """
        git = origin.git
        tips = _refs(git, f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
        if f"refs/heads/{branch}" not in tips:
            raise MemberCapabilityError(f"Branch '{branch}' has no commit to push.")
        if tips.get(f"refs/remotes/origin/{branch}") == tips[f"refs/heads/{branch}"]:
            return False, []
        listed = git(
            "log",
            "--format=%H%x00%s",
            f"refs/heads/{branch}",
            "--not",
            "--remotes=origin",
            "--",
        )
        commits = [
            {
                "id": sha,
                "message": subject or sha[:7],
                "url": self.github.commit_url_from_remote(origin.url, sha),
            }
            for sha, _, subject in (
                line.partition("\0") for line in listed.stdout.decode().splitlines()
            )
        ]
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"
        with _git_auth_environment(token) as env:
            result = git(
                "push",
                "--porcelain",
                "--no-follow-tags",
                "--",
                origin.url,
                refspec,
                env=env,
                ok=range(256),
            )
        if result.returncode != 0:
            reason = (result.stdout + result.stderr).decode(errors="replace").strip()
            raise MemberCapabilityError(
                f"Failed to push '{branch}' to origin: {reason}"
            )
        return True, commits


def _name(value: str, what: str) -> str:
    """``value`` as an owner or repository name, safe as a directory name.

    Raises:
        MemberCapabilityError: If it is not one.
    """
    if not NAME.fullmatch(value) or value in {".", ".."}:
        raise MemberCapabilityError(f"Invalid {what} name: {value!r}")
    return value


def _refs(git: _Git, *prefixes: str) -> dict[str, str]:
    """The repository's references under ``prefixes`` (all without), by name."""
    listed = git("for-each-ref", "--format=%(refname) %(objectname)", *prefixes)
    return {
        ref: name
        for ref, _, name in (
            line.partition(" ") for line in listed.stdout.decode().splitlines()
        )
    }


def _current_branch(git: _Git) -> str:
    head = git("symbolic-ref", "-q", "HEAD", ok=range(256)).stdout.decode().strip()
    if not head.startswith("refs/heads/"):
        raise MemberCapabilityError("The repository is not on a branch.")
    return head.removeprefix("refs/heads/")


def _object_name(output: bytes) -> str:
    name = output.decode(errors="replace").strip()
    if not _OBJECT_NAME.fullmatch(name):
        raise MemberCapabilityError(f"Invalid object name: {name!r}")
    return name


def _present(clone: _GuestGit, names: Iterable[str]) -> set[str]:
    """Which of ``names`` the clone holds, as far as it says."""
    asked = set(names)
    listed = clone(
        "cat-file",
        "--batch-check=%(objectname)",
        stdin="".join(f"{name}\n" for name in sorted(asked)).encode(),
    )
    # A name it holds comes back alone; one it lacks, as "<name> missing".
    return asked & set(listed.stdout.decode(errors="replace").splitlines())


def _send(origin: _Origin, clone: _GuestGit, refs: Mapping[str, str]) -> None:
    """Give the clone the host's references and the history it lacks of them."""
    held = _present(clone, refs.values())
    wanted = sorted(ref for ref, name in refs.items() if name not in held)
    if wanted:
        with tempfile.TemporaryDirectory(prefix="guildbotics-bundle-") as tmp:
            bundle = Path(tmp, "send.bundle")
            origin.git(
                "bundle",
                "create",
                "-q",
                str(bundle),
                "--stdin",
                stdin="".join(
                    f"{line}\n"
                    for line in (*wanted, *(f"^{name}" for name in sorted(held)))
                ).encode(),
            )
            clone.run(("sh", "-c", _UNBUNDLE), stdin=bundle)
    clone.run(
        ("sh", "-c", _SET_REFS),
        stdin="".join(
            f"update {ref} {name}\n" for ref, name in sorted(refs.items())
        ).encode(),
    )


def _exchange(origin: _Origin, user: _Git, repo: Path, branch: str) -> str:
    """Bring the user's own repository and the host's up to date with each
    other, and name the commit ``branch`` is pushed at.

    The user's repository is given what the host fetched of the remote, as
    its own ``git fetch`` would have; the host takes ``branch`` from the
    repository's path, and only its own ``refs/heads/<branch>`` changes.
    Neither carries a credential.

    Raises:
        MemberCapabilityError: If the branch has no commit.
    """
    ref = f"refs/heads/{branch}"
    user(
        "fetch",
        "-q",
        "--no-tags",
        "--",
        str(origin.git.cwd),
        "+refs/remotes/origin/*:refs/remotes/origin/*",
    )
    if user("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", ok=(0, 1)).returncode:
        raise MemberCapabilityError(f"Branch '{branch}' has no commit to push.")
    origin.git("fetch", "-q", "--no-tags", "--", str(repo), f"+{ref}:{ref}")
    return origin.head(branch)


def _receive(clone: _GuestGit, origin: _Origin, branch: str) -> str:
    """Take the clone's ``branch`` into the host's repository.

    The host takes history, never references: only its own
    ``refs/heads/<branch>`` is updated, and what it names is read back from
    there.
    """
    ref = f"refs/heads/{branch}"
    sha = _object_name(clone("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").stdout)
    if origin.has_commit(sha):
        origin.git("update-ref", ref, sha)
        return origin.head(branch)
    held = _present(clone, origin.refs().values())
    with tempfile.TemporaryDirectory(prefix="guildbotics-bundle-") as tmp:
        bundle = Path(tmp, "receive.bundle")
        clone(
            "bundle",
            "create",
            "-q",
            "-",
            "--stdin",
            stdin="".join(
                f"{line}\n" for line in (ref, *(f"^{name}" for name in sorted(held)))
            ).encode(),
            stdout=bundle,
        )
        # The bundle is the clone's to write: of what it names, only the
        # branch is taken, never the tags git would otherwise follow.
        origin.git("fetch", "-q", "--no-tags", "--", str(bundle), f"+{ref}:{ref}")
    return origin.head(branch)


def _check_out(
    clone: _GuestGit, branch: str, default_branch: str, refs: Mapping[str, str]
) -> None:
    """Check ``branch`` out afresh, as ``prepare`` does every time.

    What is not committed is dropped. A branch on the remote follows it,
    keeping local commits it only lacks; one that diverged is reset to it.
    A new branch starts from the default branch, which is brought up to date.
    """
    remote = f"refs/remotes/origin/{branch}"
    clone("clean", "-fdq")
    local = clone("rev-parse", "-q", "--verify", f"refs/heads/{branch}", ok=(0, 1))
    if local.returncode == 0:
        clone("checkout", "-q", "-f", branch, "--")
        if (
            remote in refs
            and clone("merge", "-q", "--ff-only", remote, ok=range(256)).returncode
        ):
            clone("reset", "-q", "--hard", remote)
    else:
        start = remote if remote in refs else f"refs/remotes/origin/{default_branch}"
        clone("checkout", "-q", "-f", "-b", branch, start, "--")
    if remote in refs:
        clone("branch", "-q", f"--set-upstream-to={remote}", branch)
    if default_branch != branch:
        # Best effort: a worktree of the clone may have it checked out.
        clone(
            "branch",
            "-q",
            "-f",
            default_branch,
            f"refs/remotes/origin/{default_branch}",
            ok=range(256),
        )


@contextmanager
def _git_auth_environment(token: str) -> Iterator[dict[str, str]]:
    """What git authenticates to the code host with, as the member."""
    if not token:
        yield {}
        return
    askpass_path = create_git_askpass_script()
    try:
        yield build_git_auth_environment(askpass_path, token)
    finally:
        askpass_path.unlink(missing_ok=True)
