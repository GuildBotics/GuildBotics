"""Immutable per-worker Git seeds for tests that need real repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shutil import copytree

from git import Repo

from guildbotics.sync.local_repository import GITIGNORE_CONTENT
from guildbotics.utils.workspace_sync_port import dump_shared_json
from guildbotics.workspace.identity import WorkspaceIdentity

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


@dataclass(frozen=True)
class WorkerGitSeed:
    """Repositories built once and copied into isolated per-test directories."""

    root: Path
    empty_hub: Path
    sync_device: Path
    member_remote: Path
    member_worktree: Path
    _snapshot: dict[str, bytes]

    @classmethod
    def create(cls, root: Path) -> WorkerGitSeed:
        empty_hub = root / "empty.git"
        with Repo.init(empty_hub, bare=True, initial_branch="main") as repository:
            repository.git.config("receive.denyNonFastForwards", "true")

        sync_source = root / "sync-source"
        with Repo.init(sync_source, initial_branch="main") as repository:
            _configure_identity(repository, "GuildBotics", "sync@guildbotics.invalid")
            (sync_source / ".gitignore").write_text(GITIGNORE_CONTENT, encoding="utf-8")
            identity = sync_source / "state" / "workspace.json"
            identity.parent.mkdir(parents=True)
            identity.write_text(
                dump_shared_json(
                    WorkspaceIdentity(
                        workspace_id=WORKSPACE_ID,
                        created_at="2026-08-01T00:00:00Z",
                    ).model_dump()
                ),
                encoding="utf-8",
            )
            repository.git.add(A=True)
            repository.index.commit("Initialize shared workspace")

        sync_hub = root / "sync.git"
        with Repo.clone_from(sync_source, sync_hub, bare=True) as repository:
            repository.git.config("receive.denyNonFastForwards", "true")
        sync_device = root / "sync-device"
        with Repo.clone_from(sync_hub, sync_device, branch="main") as repository:
            _configure_identity(repository, "GuildBotics", "sync@guildbotics.invalid")
        # ``.*`` deliberately ignores the ignore file itself, so it is not in
        # the commit cloned above. Production writes it beside the repository
        # after initialization; keep the seed in that same state.
        (sync_device / ".gitignore").write_text(GITIGNORE_CONTENT, encoding="utf-8")

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
            empty_hub=empty_hub,
            sync_device=sync_device,
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
