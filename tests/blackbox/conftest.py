"""Fresh server and local Git remote fixtures for blackbox journeys."""

import subprocess
from pathlib import Path

import pytest

from tests.blackbox.harness import running_stack


@pytest.fixture
def stack(tmp_path: Path):
    with running_stack(tmp_path) as instance:
        yield instance


@pytest.fixture
def repository(stack, tmp_path: Path):
    """A local provider's bare remote with a real feature commit."""
    seed = tmp_path / "seed"
    seed.mkdir()

    def git(*arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Blackbox",
                "-c",
                "user.email=blackbox@example.test",
                *arguments,
            ],
            cwd=seed,
            env=stack.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "base")
    git("switch", "-q", "-c", "feature")
    (seed / "README.md").write_text("feature\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "feature")
    head = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    remote = (
        stack.workspace
        / ".guildbotics/local/services/code/acme%2Fteam%2Fdemo/repository.git"
    )
    remote.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(seed), str(remote))
    return head
