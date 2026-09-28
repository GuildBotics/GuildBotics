"""Member git runs a member's clone only inside the command's microVM.

Skipped unless ``GUILDBOTICS_CONTRACT_PROBE=1``, on a device whose agent
environment is ready (point ``GUILDBOTICS_CONFIG_DIR`` at a workspace with a
built snapshot). It boots the snapshot working in a directory of clones,
prepares one from a repository on this device, commits in it with a
pre-commit hook the clone carries, and pushes: the hook runs inside the
microVM, and the push reaches the repository through the host's own one. It
sends nothing off the device.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import git
import pytest

from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.entities.team import Person, Project, Team
from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.spec import build_environment_spec
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime.command_guest import EnvironmentGuest
from guildbotics.intelligences.agent_runtime.environment import CODE_MOUNT, CODE_ROOT
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from tests.git_seed import WorkerGitSeed
from tests.timeouts import REAL_DEVICE

#: The home the snapshot was built with; the suite's own fixtures move HOME.
_REAL_HOME = Path.home()

pytestmark = [
    *REAL_DEVICE,
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_CONTRACT_PROBE") != "1",
        reason="Set GUILDBOTICS_CONTRACT_PROBE=1 to probe the agent environment.",
    ),
    pytest.mark.asyncio,
]

#: A hook that says where it ran: only the microVM has GuildBotics' code there.
_HOOK = f"""#!/bin/sh
if [ -d {CODE_ROOT} ]; then where=inside; else where=host; fi
echo "$where" > "$(git rev-parse --git-dir)/hook-ran"
"""


async def test_prepare_commit_and_push_run_the_clone_in_the_microvm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_git_seed: WorkerGitSeed
) -> None:
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(_REAL_HOME))
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "dummy-not-a-secret")
    status = device_status()
    if status.refusal or status.snapshot is None or status.declaration is None:
        pytest.skip(f"The agent environment is not ready here: {status.refusal}")
    remote = tmp_path / "remote.git"
    worker_git_seed.copy(worker_git_seed.member_remote, remote)
    clones = tmp_path / "clones"
    clones.mkdir()
    person = Person(
        person_id="aiko",
        name="Aiko",
        account_info={"git_user": "Aiko Bot", "git_email": "aiko@example.com"},
    )
    service = MemberGitWorkspaceService(
        person, Team(project=Project(name="probe"), members=[person])
    )
    service.workspace_root = clones
    service.host_root = tmp_path / "host"

    async def default_branch(owner: str, repo: str) -> str:
        return "main"

    async def clone_url(owner: str, repo: str) -> str:
        return remote.as_uri()

    monkeypatch.setattr(service.github, "default_branch", default_branch)
    monkeypatch.setattr(service.github, "get_clone_url", clone_url)
    environment = await AgentEnvironment.start(
        build_environment_spec(
            AccessContract(),
            clones,
            home=_REAL_HOME,
            nameservers=status.dns.nameservers,
            mounts=(CODE_MOUNT,),
        ),
        snapshot=str(status.snapshot.path),
        memory_mib=status.declaration.resources.memory_mib,
        cpus=status.declaration.resources.cpus,
    )
    guest = EnvironmentGuest(asyncio.get_running_loop(), lambda: environment).until(
        time.monotonic() + 300
    )

    def member(operation):
        """Run ``operation`` the way the broker runs a member command: on a
        thread of its own, with the command's microVM."""

        async def invoke():
            with member_invocation_scope(MemberInvocation(guest=guest)):
                return await operation()

        return asyncio.to_thread(asyncio.run, invoke())

    try:
        prepared = await member(
            lambda: service.prepare(repo="owner/repo", branch="probe")
        )
        clone = Path(prepared["repo_path"])
        hook = clone / ".git" / "hooks" / "pre-commit"
        hook.write_text(_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        (clone / "probe.txt").write_text("probe\n", encoding="utf-8")
        staged = await asyncio.to_thread(
            guest.run,
            ["git", "add", "probe.txt"],
            cwd=guest.path(clone),
            env={},
            stdout_limit=1 << 20,
        )
        assert staged.returncode == 0, staged.stderr

        published = await member(lambda: service.publish(clone, "probe commit"))
    finally:
        await environment.close()

    assert (clone / ".git" / "hook-ran").read_text(encoding="utf-8").strip() == "inside"
    assert published.pushed is True
    with git.Repo(remote) as pushed:
        assert pushed.commit("probe").hexsha == published.commit_sha
        assert pushed.commit("probe").author.email == "aiko@example.com"
    with git.Repo(clone) as repo:
        assert repo.commit("refs/remotes/origin/probe").hexsha == published.commit_sha
