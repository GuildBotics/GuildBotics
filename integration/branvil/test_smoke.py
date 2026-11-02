"""Branvil and GuildBotics' ``branvil`` provider, connected for real.

Each test starts its own ``branvil serve`` (``BRANVIL_BIN``) on a free
loopback port with a data directory in a temporary directory, and drives it
with the production provider that GuildBotics' factory makes. It walks one of
the three paths where the two products meet over HTTP and Git:

- tickets: create, update, and read back; the owner assigns one and puts it
  in the ready lane; a parent and a ticket it is blocked by read back from
  both sides; a ready ticket blocked by an open one is no patrol candidate and
  cannot be moved in progress, and is one once its blocker is closed; the
  relations are taken back;
- Git, pull request, and merge: the member pushes with the clone URL and
  credential the provider gives, the provider opens a pull request that
  closes a ticket, the owner merges it through the API, and the provider reads
  the pull request merged and the ticket closed in the done lane;
- checks: ``main`` holds a pipeline of one job; the pull request is not ready
  while its head's job is pending, is ready once a runner (this test, through
  the runner API) finishes that commit's job, and is not ready again when its
  head moves on; a head behind the base exposes its gate's queued, successful,
  failed, or conflicting check through the API and the provider.

The owner's setup and merge, the runner's claim and finish, and the gate's
wire checks go to the API directly; provider checks are compared with them.
Everything else the provider does is covered by GuildBotics' own port
contracts and Branvil's tests. Run it with ``scripts/branvil-integration.sh``.

Client-side Git never reads the user's configuration: ``GIT_CONFIG_NOSYSTEM``,
an empty ``GIT_CONFIG_GLOBAL``, ``GIT_TERMINAL_PROMPT=0``, ``credential.helper``
cleared, a ``HOME`` of its own, credentials only from environment variables
read by an askpass script.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.runtime.code_hosting_service import CodeHostingService, TicketLink
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.ticket_service import TicketService

OWNER = "alice"
MEMBER = "aiko"
RUNNER = "runner1"
DIGEST = "sha256:" + "2" * 64
SCOPE = "acme"
REPO = f"{SCOPE}/demo"
PIPELINE = """[[jobs]]
name = "test"
image = "docker.io/library/alpine:3.22"
commands = ["true"]
"""
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$BRANVIL_GIT_USER" ;;
  *) printf '%s\\n' "$BRANVIL_GIT_TOKEN" ;;
esac
"""


class Service:
    """A real ``branvil serve`` with its data directory under ``root``."""

    def __init__(self, root: Path) -> None:
        binary = os.environ.get("BRANVIL_BIN")
        assert binary, "set BRANVIL_BIN to the branvil binary"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "TMPDIR": str(root),
        }
        data = root / "data"
        done = subprocess.run(
            [binary, "init", "--owner", OWNER, "--listen", "127.0.0.1:0"]
            + ["--data-dir", str(data)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, f"branvil init: {done.stderr}"
        self.owner = (data / "secrets" / "owner.token").read_text("utf-8").strip()
        self.log = root / "serve.log"
        with self.log.open("ab") as log:
            self._process = subprocess.Popen(
                [binary, "serve", "--data-dir", str(data)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=log,
                stdin=subprocess.DEVNULL,
                text=True,
            )
        try:
            assert self._process.stdout is not None
            first = self._process.stdout.readline()
            found = re.match(r"branvil listening on (\S+)", first)
            assert found, f"unexpected first line {first!r}; log: {self.log_tail()}"
        except BaseException:
            self._end()
            raise
        self.url = found.group(1)
        self.http = httpx.Client(
            base_url=f"{self.url}/api/v1", timeout=30, trust_env=False
        )

    def log_tail(self) -> str:
        return self.log.read_text("utf-8", errors="replace")[-20000:]

    def stop(self) -> None:
        self.http.close()
        self._end()

    def _end(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def call(self, method: str, path: str, token: str, body: Any = None) -> Any:
        response = self.http.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
        return response.json() if response.content else None

    def member(self, name: str, kind: str) -> str:
        """Create a member; returns a new token of it."""
        self.call("POST", "/members", self.owner, {"name": name, "kind": kind})
        return str(
            self.call("POST", f"/members/{name}/tokens", self.owner, {"label": "t"})[
                "token"
            ]
        )


class Git:
    """A working copy that pushes to the service as whoever ``push`` names."""

    def __init__(self, root: Path) -> None:
        self.home = root / "git-home"
        self.home.mkdir(parents=True)
        (self.home / "gitconfig").write_text("", encoding="utf-8")
        self.askpass = self.home / "askpass.sh"
        self.askpass.write_text(_ASKPASS, encoding="utf-8")
        self.askpass.chmod(0o700)
        self.work = root / "work"
        self.run("init", "-q", "-b", "main", str(self.work))

    def run(self, *args: str, user: str = OWNER, password: str = "") -> str:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / "gitconfig"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(self.askpass),
            "BRANVIL_GIT_USER": user,
            "BRANVIL_GIT_TOKEN": password,
            "GIT_AUTHOR_NAME": user,
            "GIT_AUTHOR_EMAIL": f"{user}@example.invalid",
            "GIT_COMMITTER_NAME": user,
            "GIT_COMMITTER_EMAIL": f"{user}@example.invalid",
        }
        done = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "core.askPass=", *args],
            cwd=self.work if self.work.exists() else self.home,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
        return done.stdout.strip()

    def commit(self, start: str, files: dict[str, str]) -> str:
        """Commit ``files`` on ``start`` (empty: no parent yet); returns the
        commit."""
        if start:
            self.run("checkout", "-q", "--detach", start)
        for path, content in files.items():
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            (self.work / path).write_text(content, encoding="utf-8")
        self.run("add", ".")
        self.run("commit", "-q", "-m", "change")
        return self.run("rev-parse", "HEAD")

    def push(self, remote: str, sha: str, branch: str, user: str, password: str) -> None:
        self.run("push", "-q", remote, f"{sha}:refs/heads/{branch}", user=user, password=password)


@dataclass
class World:
    service: Service
    git: Git
    runner: str
    #: ``main``'s first commit, which holds the pipeline.
    main: str
    team: Team
    person: Person

    def seg(self) -> str:
        return quote(REPO, safe="")

    def ticket_api(self, url: str) -> str:
        number = url.rpartition("/-/tickets/")[2]
        return f"/repos/{self.seg()}/tickets/{number}"

    def assign(self, url: str) -> None:
        """The owner assigns the member and puts the ticket in the ready lane."""
        api = self.ticket_api(url)
        self.service.call("PUT", f"{api}/assignee", self.service.owner, {"name": MEMBER})
        self.service.call("PUT", f"{api}/board", self.service.owner, {"lane": "ready"})

    def merge(self, url: str, head: str) -> None:
        """The owner approves the member's pull request at ``head`` (a new
        repository needs one approval) and merges it."""
        number = url.rpartition("/-/pulls/")[2]
        self.service.call(
            "POST",
            f"/repos/{self.seg()}/pulls/{number}/reviews",
            self.service.owner,
            {"verdict": "approve", "commit": head},
        )
        self.service.call(
            "POST",
            f"/repos/{self.seg()}/pulls/{number}/merge",
            self.service.owner,
            {"expected_head": head, "expected_base": self.main},
        )

    def finish_job(self, sha: str, conclusion: str = "success") -> None:
        """Finish queued attempts until ``sha`` concludes with ``conclusion``;
        other commits succeed."""
        while True:
            response = self.service.http.post(
                "/runner/claim",
                json={"executor": "microsandbox", "executor_state": "ready"},
                headers={"Authorization": f"Bearer {self.runner}"},
            )
            assert response.status_code == httpx.codes.OK, f"no queued job of {sha}"
            attempt = response.json()
            self.service.call(
                "POST",
                f"/runner/attempts/{attempt['attempt_id']}/finish",
                self.runner,
                {
                    "lease": attempt["lease"],
                    "conclusion": conclusion if attempt["sha"] == sha else "success",
                    "image_digest": DIGEST,
                },
            )
            if attempt["sha"] == sha:
                return

    def run(
        self,
        scenario: Callable[[CodeHostingService, TicketService], Awaitable[None]],
    ) -> None:
        """Run ``scenario`` with the provider's code host and ticket service."""

        async def main() -> None:
            factory = ServiceIntegrationFactory()
            logger = logging.getLogger(__name__)
            code = factory.create_code_hosting_service(logger, self.person, self.team)
            tickets = factory.create_ticket_service(logger, self.person, self.team)
            try:
                # Nothing of this test may reach another Branvil, such as
                # one running on this machine at the default address.
                remote = await code.clone_url(REPO)
                assert remote.startswith(f"{self.service.url}/"), remote
                await scenario(code, tickets)
            finally:
                await code.aclose()
                await tickets.aclose()

        asyncio.run(main())


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    with tempfile.TemporaryDirectory(prefix="branvil-smoke-") as tmp:
        root = Path(tmp)
        service = Service(root / "branvil")
        try:
            # GuildBotics reads where Branvil is from the identities'
            # connection; nothing else of this machine's GuildBotics is used,
            # and no proxy stands between the provider and the service.
            monkeypatch.setenv("HOME", str(root / "home"))
            monkeypatch.setenv("NO_PROXY", "*")
            monkeypatch.setenv("GUILDBOTICS_IDENTITY_ROOT", str(root / "identity"))
            for name in ("GUILDBOTICS_CONFIG_DIR", "GUILDBOTICS_WORKSPACE_ROOT"):
                monkeypatch.delenv(name, raising=False)
            connection = root / "identity/.guildbotics/config/connections/branvil.yml"
            connection.parent.mkdir(parents=True)
            connection.write_text(f"base_url: {service.url}\n", encoding="utf-8")
            owner = service.owner
            monkeypatch.setenv(f"{MEMBER.upper()}_BRANVIL_TOKEN", service.member(MEMBER, "agent"))
            runner = service.member(RUNNER, "runner")
            service.call("POST", "/workspaces", owner, {"name": SCOPE})
            service.call("POST", "/repos", owner, {"path": REPO})
            git = Git(root / "git")
            main = git.commit("", {"README.md": "seed\n", ".branvil/ci.toml": PIPELINE})
            git.push(f"{service.url}/git/{REPO}.git", main, "main", OWNER, owner)
            person = Person(
                person_id=MEMBER,
                name="Aiko",
                person_type="agent",
                account_info={"branvil_username": MEMBER},
            )
            config = {"name": "branvil", "workspace": SCOPE}
            team = Team(
                project=Project(
                    name="demo",
                    services={"code_hosting_service": config, "ticket_service": config},
                ),
                members=[person],
            )
            yield World(service, git, runner, main, team, person)
        finally:
            service.stop()
            # Shown with the test's report when it fails.
            print(service.log_tail())


async def _read(tickets: TicketService, url: str) -> dict[str, Any]:
    return (await tickets.read("tickets", url)).items[0]


async def _create(tickets: TicketService, title: str) -> str:
    return (await tickets.create_ticket(REPO, title, "", [])).target.url


async def _change(tickets: TicketService, url: str, **change: Any) -> None:
    fields: dict[str, Any] = {
        "body": None,
        "title": None,
        "add_labels": (),
        "remove_labels": (),
        "state": None,
        "state_reason": None,
    }
    await tickets.update_ticket(url, **(fields | change))


async def _push(world: World, code: CodeHostingService, start: str, branch: str) -> str:
    """The member pushes a commit on ``start`` as ``branch`` with the clone URL
    and credential the provider gives."""
    sha = world.git.commit(start, {"a.py": f"# {branch} on {start}\n"})
    credential = await code.git_credential()
    assert credential is not None
    world.git.push(
        await code.clone_url(REPO), sha, branch, credential.username, credential.password
    )
    return sha


def test_initial_ticket_state_and_interactive_patrol_exclusion(world: World) -> None:
    async def scenario(_: CodeHostingService, tickets: TicketService) -> None:
        parent = await _create(tickets, "Parent")
        blocker = await _create(tickets, "Blocker")
        await _change(tickets, blocker, state="closed", state_reason="completed")
        for interactive in (False, True):
            created = await tickets.create_ticket(
                REPO, "Assigned at creation", "", [], assignee=MEMBER,
                interactive=interactive, lane="in_progress", parent=parent,
                blocked_by=[blocker],
            )
            ticket = await _read(tickets, created.target.url)
            assert ticket["assignees"] == [MEMBER]
            assert ticket["lane"] == "in_progress"
            assert ticket["parent"]["url"] == parent
            assert [b["url"] for b in ticket["blocked_by"]] == [blocker]
            candidate_urls = [t.url for t in await tickets.candidates()]
            assert (created.target.url in candidate_urls) is not interactive
            task = Task(id=created.target.url, url=created.target.url,
                        title="Assigned at creation", description="", status=Task.IN_PROGRESS)
            assert (await tickets.refresh(task) is None) is interactive
            child = await tickets.create_ticket(
                REPO, "Child", "", [], assignee=MEMBER, parent=created.target.url,
            )
            assert (await _read(tickets, child.target.url))["lane"] == "ready"
            assert (child.target.url in [t.url for t in await tickets.candidates()]) is not interactive

    world.run(scenario)


def test_tickets_and_their_relations(world: World) -> None:
    async def scenario(_: CodeHostingService, tickets: TicketService) -> None:
        created = await tickets.create_ticket(REPO, "Fix login", "It fails.", [])
        url = created.target.url
        assert url == f"{world.service.url}/{REPO}/-/tickets/1"
        await _change(tickets, url, body="It still fails.", title="Fix the login")
        world.assign(url)
        ticket = await _read(tickets, url)
        assert (
            ticket["url"],
            ticket["title"],
            ticket["body"],
            ticket["state"],
            ticket["assignees"],
            ticket["lane"],
        ) == (url, "Fix the login", "It still fails.", "open", [MEMBER], "ready")

        parent, blocker = await _create(tickets, "Parent"), await _create(tickets, "Blocker")
        await _change(tickets, url, parent=parent, add_blocked_by=[blocker])
        ticket = await _read(tickets, url)
        assert ticket["parent"]["url"] == parent
        assert [(b["url"], b["state"]) for b in ticket["blocked_by"]] == [(blocker, "open")]
        assert [c["url"] for c in (await _read(tickets, parent))["children"]] == [url]
        assert [b["url"] for b in (await _read(tickets, blocker))["blocks"]] == [url]

        assert await tickets.candidates() == []
        task = Task(id=url, url=url, title="Fix the login", description="", status=Task.READY)
        with pytest.raises(ServiceError):
            await tickets.move(task, "in_progress")
        assert (await _read(tickets, url))["lane"] == "ready"

        await _change(tickets, blocker, state="closed", state_reason="completed")
        [task] = await tickets.candidates()
        assert task.url == url
        assert await tickets.move(task, "in_progress") is True
        assert (await _read(tickets, url))["lane"] == "in_progress"

        await _change(tickets, url, parent="", remove_blocked_by=[blocker])
        ticket = await _read(tickets, url)
        assert (ticket["parent"], ticket["blocked_by"]) == (None, [])
        assert (await _read(tickets, parent))["children"] == []
        assert (await _read(tickets, blocker))["blocks"] == []

        # The GUI's edits and deletions are seen through the existing provider
        # reads and by the candidate refresh just before dispatch.
        written = await tickets.comment(url, "Original comment")
        [before] = (await tickets.read("ticket_comments", url)).items
        path = f"/repos/{quote(REPO, safe='')}/tickets/1/comments/{written.id}"
        world.service.call(
            "PATCH", path, world.service.owner, {"body": "Edited comment"}
        )
        [edited] = (await tickets.read("ticket_comments", url)).items
        assert edited == {**before, "body": "Edited comment"}
        # A human's later comment makes this working ticket a candidate.
        world.service.call(
            "POST", path.rsplit("/", 1)[0], world.service.owner, {"body": "Continue"}
        )
        refreshed = await tickets.refresh(task)
        assert refreshed is not None
        assert [c.content for c in refreshed.comments] == ["Edited comment", "Continue"]
        world.service.call("DELETE", path, world.service.owner)
        [remaining] = (await tickets.read("ticket_comments", url)).items
        assert remaining["body"] == "Continue"
        refreshed = await tickets.refresh(task)
        assert refreshed is not None
        assert [c.content for c in refreshed.comments] == ["Continue"]

    world.run(scenario)


def test_mention_shapes_and_workspace_discovery(world: World) -> None:
    async def scenario(_: CodeHostingService, tickets: TicketService) -> None:
        ticket = await _create(tickets, "Unassigned summons")
        await _change(tickets, ticket, body=f"@{MEMBER.upper()} @{MEMBER}")
        api = world.ticket_api(ticket)
        token = os.environ[f"{MEMBER.upper()}_BRANVIL_TOKEN"]
        wire = world.service.call("GET", api, token)
        assert wire["mentions"] == [MEMBER]
        await tickets.comment(ticket, f"@{OWNER} please confirm")
        comments = world.service.call("GET", f"{api}/comments", token)
        assert comments["items"][0]["mentions"] == [OWNER]
        discovery = f"/workspaces/{SCOPE}/tickets?mentions=me"
        page = world.service.call("GET", discovery, token)
        assert [item["url"] for item in page["items"]] == [ticket]
        assert page["items"][0]["assignee"] is None
        assert page["items"][0]["comments"] == comments["items"]
        await _change(tickets, ticket, body="Mention removed")
        assert world.service.call("GET", discovery, token)["items"] == []

        # The provider uses the same stored mention data when work is assigned.
        world.assign(ticket)
        world.service.call("PUT", f"{api}/board", world.service.owner, {"lane": "in_progress"})
        await _change(tickets, ticket, body=f"@{MEMBER.upper()}")
        # Its own earlier comment predates assignment and does not answer it.
        [task] = await tickets.candidates()
        assert (task.url, task.trigger_reason) == (ticket, "issue_mention")

    world.run(scenario)


def test_a_push_a_pull_request_and_its_merge(world: World) -> None:
    async def scenario(code: CodeHostingService, tickets: TicketService) -> None:
        ticket = await _create(tickets, "Fix login")
        head = await _push(world, code, world.main, "feature")
        created = await code.create_pull_request(
            REPO,
            "feature",
            "",
            "Fix login",
            "Body",
            draft=False,
            tickets=[TicketLink(ticket, closes=True)],
        )
        url = created.target.url
        [pull] = (await code.read("pull_requests", url)).items
        assert (pull["head"], pull["base"], pull["head_sha"], pull["state"]) == (
            "feature",
            "main",
            head,
            "open",
        )
        referring = await code.read("ticket_pull_requests", ticket)
        assert [p["url"] for p in referring.items] == [url]

        world.merge(url, head)

        [pull] = (await code.read("pull_requests", url)).items
        assert pull["merged"] is True
        read = await _read(tickets, ticket)
        assert (read["state"], read["lane"]) == ("closed", "done")

    world.run(scenario)


def test_open_pull_requests_follow_newest_first_pages(world: World) -> None:
    async def scenario(code: CodeHostingService, _: TicketService) -> None:
        await _push(world, code, world.main, "feature")
        credential = await code.git_credential()
        assert credential is not None
        # Distinct bases allow >100 open PRs on one head, crossing the
        # provider's page size without creating hundreds of commits.
        world.git.run(
            "push", "-q", await code.clone_url(REPO),
            *(f"{world.main}:refs/heads/base-{i}" for i in range(101)),
            user=credential.username, password=credential.password,
        )
        created = []
        for i in range(101):
            result = await code.create_pull_request(
                REPO, "feature", f"base-{i}", f"Change {i}", "",
                draft=False, tickets=[],
            )
            created.append(result.target.url)
        found = await code.open_pull_requests(REPO, "feature")
        assert found == list(reversed(created))
        assert await code.open_pull_requests(REPO, "missing") == []

    world.run(scenario)


def test_checks_decide_readiness_of_the_current_head(world: World) -> None:
    async def scenario(code: CodeHostingService, _: TicketService) -> None:
        head = await _push(world, code, world.main, "feature")
        created = await code.create_pull_request(
            REPO, "feature", "", "Fix login", "", draft=False, tickets=[]
        )
        url = created.target.url

        pending = await code.readiness(url)
        assert (pending.head_sha, pending.rollup, pending.readiness) == (
            head,
            "pending",
            "blocked",
        )

        world.finish_job(head)
        ready = await code.readiness(url)
        assert (ready.head_sha, ready.rollup, ready.readiness) == (head, "success", "ready")

        moved = await _push(world, code, head, "feature")
        again = await code.readiness(url)
        assert (again.head_sha, again.rollup, again.readiness) == (
            moved,
            "pending",
            "blocked",
        )

    world.run(scenario)


def test_a_reused_result_is_ready_through_the_provider(world: World) -> None:
    async def scenario(code: CodeHostingService, _: TicketService) -> None:
        world.finish_job(world.main)
        world.git.run("commit", "-q", "--allow-empty", "-m", "same tree")
        head = world.git.run("rev-parse", "HEAD")
        credential = await code.git_credential()
        assert credential is not None
        world.git.push(await code.clone_url(REPO), head, "same", credential.username, credential.password)
        created = await code.create_pull_request(
            REPO, "same", "", "Same tree", "", draft=False, tickets=[]
        )
        response = world.service.http.post(
            "/runner/claim",
            json={"executor": "microsandbox", "executor_state": "ready"},
            headers={"Authorization": f"Bearer {world.runner}"},
        )
        assert response.status_code == httpx.codes.OK
        attempt = response.json()
        source = attempt["reuse"]
        assert source["sha"] == world.main and source["image_digest"] == DIGEST
        world.service.call(
            "POST", f"/runner/attempts/{attempt['attempt_id']}/finish", world.runner,
            {"lease": attempt["lease"], "conclusion": "success", "image_digest": DIGEST,
             "reused_from": source["attempt_id"]},
        )
        ready = await code.readiness(created.target.url)
        assert (ready.head_sha, ready.rollup, ready.readiness) == (head, "success", "ready")
        assert len(ready.checks) == 1

    world.run(scenario)


@pytest.mark.parametrize("conclusion", ["success", "failure", "conflict"])
def test_a_behind_head_exposes_the_landing_gate_checks(
    world: World, conclusion: str
) -> None:
    async def scenario(code: CodeHostingService, _: TicketService) -> None:
        repo_api = f"/repos/{world.seg()}"
        service = world.service
        repo = service.call("GET", repo_api, service.owner)
        service.call(
            "PATCH",
            repo_api,
            service.owner,
            {"revision": repo["revision"], "required_checks": ["test"]},
        )
        head = await _push(world, code, world.main, "feature")
        created = await code.create_pull_request(
            REPO, "feature", "", "Behind the base", "", draft=False, tickets=[]
        )
        world.finish_job(head)
        # Advance the base without changing or approving the pull request's head.
        files = (
            {"a.py": "# a conflicting base change\n"}
            if conclusion == "conflict"
            else {"base.txt": "another change\n"}
        )
        base = world.git.commit(world.main, files)
        credential = await code.git_credential()
        assert credential is not None
        world.git.push(
            await code.clone_url(REPO),
            base,
            "advance-base",
            credential.username,
            credential.password,
        )
        other = await code.create_pull_request(
            REPO, "advance-base", "", "Advance the base", "", draft=False, tickets=[]
        )
        world.finish_job(base)
        world.merge(other.target.url, base)
        number = created.target.url.rpartition("/-/pulls/")[2]
        pull_api = f"{repo_api}/pulls/{number}"
        service.call(
            "POST",
            f"{pull_api}/reviews",
            service.owner,
            {"verdict": "approve", "commit": head},
        )

        async def landing(state: str) -> dict[str, Any]:
            for _ in range(100):
                pull = service.call("GET", pull_api, service.owner)
                entry = pull.get("landing")
                if entry and entry["state"] == state:
                    return entry
                await asyncio.sleep(0.1)
            pytest.fail(f"landing did not become {state}: {pull}")

        async def gate_check(name: str, status: str) -> dict[str, Any]:
            for _ in range(100):
                report = service.call("GET", f"{pull_api}/checks", service.owner)
                for check in report["checks"]:
                    if check["name"] == name and check["status"] == status:
                        # A gating entry precedes CI registration. Wait for its
                        # actual attempt before asking the runner to claim it.
                        if status != "queued" or check.get("attempt_id"):
                            return check
                await asyncio.sleep(0.1)
            pytest.fail(f"{name} did not become {status}: {report}")

        if conclusion == "conflict":
            entry = await landing("left")
            assert entry["reason"]["files"] == ["a.py"]
        else:
            entry = await landing("gating")
            queued = await gate_check("gate/test", "queued")
            assert queued["conclusion"] is None
            pending = await code.readiness(created.target.url)
            assert pending.behind_by == 1 and pending.out_of_date is True
            [gate] = [c for c in pending.checks if c.name == "gate/test"]
            assert (gate.status, gate.conclusion) == ("queued", None)
            world.finish_job(entry["gate"], conclusion)
            await landing("passed" if conclusion == "success" else "left")

        name = "gate" if conclusion == "conflict" else "gate/test"
        wire = await gate_check(name, "completed")
        expected = "success" if conclusion == "success" else "failure"
        assert wire["conclusion"] == expected
        if conclusion == "conflict":
            assert "a.py" in wire["details"]
        result = await code.readiness(created.target.url)
        assert result.head_sha == head
        assert result.behind_by == 1 and result.out_of_date is True
        [gate] = [c for c in result.checks if c.name == name]
        assert (gate.status, gate.conclusion) == (wire["status"], wire["conclusion"])

    world.run(scenario)
