"""Immutable per-worker Git seeds for tests that need real repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shutil import copytree

from git import Repo


@dataclass(frozen=True)
class WorkerGitSeed:
    """Repositories built once and copied into isolated per-test directories."""

    root: Path
    member_remote: Path
    member_worktree: Path
    _snapshot: dict[str, bytes]

    @classmethod
    def create(cls, root: Path) -> WorkerGitSeed:
        member_source = root / "member-source"
        with Repo.init(member_source, initial_branch="main") as repository:
            _configure_identity(repository, "Seed", "seed@example.com")
            (member_source / "README.md").write_text("initial\n", encoding="utf-8")
            repository.git.add(A=True)
            repository.index.commit("initial")

        member_remote = root / "member.git"
        with Repo.clone_from(member_source, member_remote, bare=True):
            pass
        member_worktree = root / "member-worktree"
        with Repo.clone_from(
            member_remote, member_worktree, branch="main"
        ) as repository:
            _configure_identity(repository, "Existing", "existing@example.com")

        snapshot = _snapshot(root)
        return cls(
            root=root,
            member_remote=member_remote,
            member_worktree=member_worktree,
            _snapshot=snapshot,
        )

    def copy(self, source: Path, destination: Path) -> None:
        """Copy a closed seed into an independent repository."""
        copytree(source, destination)

    def assert_unchanged(self) -> None:
        """Prove tests did not leak refs, config, worktree, or objects to the seed."""
        assert _snapshot(self.root) == self._snapshot


def _configure_identity(repository: Repo, name: str, email: str) -> None:
    with repository.config_writer() as writer:
        writer.set_value("user", "name", name)
        writer.set_value("user", "email", email)
        writer.set_value("commit", "gpgsign", "false")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
