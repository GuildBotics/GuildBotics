"""Fresh server and local Git remote fixtures for blackbox journeys."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.blackbox.harness import running_stack


@pytest.fixture
def stack(tmp_path: Path):
    with running_stack(tmp_path) as instance:
        yield instance


@dataclass
class Checkout:
    path: Path
    env: dict[str, str]

    def git(self, *arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Blackbox",
                "-c",
                "user.email=blackbox@example.test",
                *arguments,
            ],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def commit(self, name, content):
        (self.path / name).write_text(content, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", "blackbox change")
        return self.git("rev-parse", "HEAD")

    def push(self, stack):
        return stack.member(
            "git",
            "push",
            "--repo-path",
            str(self.path),
            "--workspace-mode",
            "current",
            cwd=self.path,
        )


@pytest.fixture
def checkout(stack, tmp_path: Path):
    """A throwaway checkout; provider setup alone populates the remote directly."""
    seed = tmp_path / "seed"
    seed.mkdir()
    setup = Checkout(seed, stack.env)
    setup.git("init", "-q", "-b", "main")
    setup.commit("README.md", "base\n")
    stack.provider.provision_repository(setup)
    work = tmp_path / "work"
    setup.git("clone", "-q", stack.provider.clone_url, str(work))
    result = Checkout(work, stack.env)
    result.git("switch", "-q", "-c", "feature")
    result.commit("README.md", "feature\n")
    return result


@pytest.fixture
def repository(stack, checkout):
    """A real feature commit, pushed through the public member interface."""
    checkout.push(stack)
    return checkout.git("rev-parse", "HEAD")
