"""Client-side Git needs neither the host's providers nor its capabilities."""

import subprocess
import sys

import git
import pytest

from guildbotics.local_git import GitError, LocalGit


def test_local_git_imports_without_capabilities_or_integrations():
    probe = """
import importlib.abc, sys
class NoHost(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("guildbotics.capabilities", "guildbotics.integrations")):
            raise AssertionError(fullname)
sys.meta_path.insert(0, NoHost())
from guildbotics.local_git import LocalGit
assert not any(name.startswith(("guildbotics.capabilities", "guildbotics.integrations"))
               for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


def test_branch_bundle_excludes_known_history_and_can_send_it_all(
    tmp_path, worker_git_seed
):
    path = tmp_path / "client"
    worker_git_seed.copy(worker_git_seed.member_worktree, path)
    with git.Repo(path) as repo:
        base = repo.head.commit.hexsha
        tip = repo.index.commit("local work").hexsha
    client = LocalGit(path)
    partial, full = tmp_path / "partial.bundle", tmp_path / "full.bundle"

    assert client.branch_bundle("main", [base, "0" * 40], partial)
    assert client.branch_bundle("main", [], full)
    header = partial.read_bytes().split(b"\n\n", 1)[0]
    assert f"-{base} ".encode() in header
    assert f"{tip} refs/heads/main".encode() in header
    assert not any(
        line.startswith(b"-")
        for line in full.read_bytes().split(b"\n\n", 1)[0].splitlines()
    )
    assert not client.branch_bundle("main", [tip], tmp_path / "unneeded.bundle")
    assert not (tmp_path / "unneeded.bundle").exists()


def test_tracking_update_rejects_non_tracking_refs_before_import(tmp_path):
    client = LocalGit(tmp_path)
    with pytest.raises(GitError, match="Only origin tracking"):
        client.update_tracking(
            tmp_path / "absent.bundle", {"refs/heads/main": "0" * 40}
        )
