"""The repository boundary and the Git mechanics the manager is built on."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from git import Git, GitCommandError, Repo

from guildbotics.sync.local_repository import (
    REJECTED_REF_PREFIX,
    HubTimeoutError,
    LocalSyncRepository,
    SyncRepositoryError,
    _run_remote_git,
)


def _workspace(root: Path) -> LocalSyncRepository:
    (root / ".guildbotics" / "state").mkdir(parents=True)
    (root / ".guildbotics" / "local" / "run").mkdir(parents=True)
    repository = LocalSyncRepository(root)
    repository.initialize()
    return repository


def test_initialize_ignores_device_local_data_and_env_files(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    (tmp_path / ".guildbotics" / "local" / "run" / "pid").write_text("1")
    (tmp_path / ".guildbotics" / ".env").write_text("TOKEN=x")
    (tmp_path / ".guildbotics" / "state" / "kept.json").write_text("{}")

    assert [change.path for change in repository.working_tree_changes()] == [
        "state/kept.json"
    ]
    assert (
        tmp_path / ".guildbotics" / ".gitignore"
    ).read_text() == "local/\n.env\n*.tmp\n"


def test_initialize_is_repeatable(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    head_before = repository.head()

    repository.initialize()

    assert repository.initialized
    assert repository.head() == head_before


def test_boundary_refuses_a_repository_inside_a_member_working_clone(
    tmp_path: Path,
) -> None:
    """A member clone holds the user's own branches and uncommitted work, so
    synchronization must never be able to commit inside one."""
    clone_root = tmp_path / ".guildbotics" / "local" / "clones" / "aiko"
    clone_root.mkdir(parents=True)
    repository = LocalSyncRepository(clone_root)

    with pytest.raises(SyncRepositoryError, match="member working clone"):
        repository.verify_boundary()


def test_boundary_refuses_a_workspace_that_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _workspace(tmp_path)
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path / "elsewhere"))
    moved = LocalSyncRepository()
    moved.path = repository.path

    with pytest.raises(SyncRepositoryError, match="moved"):
        moved.verify_boundary()


def test_boundary_refuses_a_directory_owned_by_another_repository(
    tmp_path: Path,
) -> None:
    """``.guildbotics`` inside an existing repository is not a sync repository:
    committing there would write into the user's own history."""
    Repo.init(tmp_path, initial_branch="main")
    (tmp_path / ".guildbotics" / "state").mkdir(parents=True)
    repository = LocalSyncRepository(tmp_path)

    with pytest.raises(SyncRepositoryError, match="not a synchronization repository"):
        repository.working_tree_changes()


def test_commit_records_writes_and_deletions(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    (tmp_path / ".guildbotics" / "state" / "a.json").write_text("{}")
    repository.stage(["state/a.json"])
    first = repository.commit("first")

    (tmp_path / ".guildbotics" / "state" / "a.json").unlink()
    repository.stage(["state/a.json"])
    second = repository.commit("second")

    assert first is not None and second is not None
    assert repository.changed_paths(first, second) == {"state/a.json": "D"}


def test_commit_without_staged_changes_creates_nothing(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    (tmp_path / ".guildbotics" / "state" / "a.json").write_text("{}")
    repository.stage(["state/a.json"])
    head = repository.commit("first")

    assert repository.commit("second") is None
    assert repository.head() == head


def test_paths_with_spaces_survive_scanning_and_diffing(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    name = "state/chat_state/a channel/thread 1.json"
    path = tmp_path / ".guildbotics" / name
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    assert [change.path for change in repository.working_tree_changes()] == [name]
    repository.stage([name])
    head = repository.commit("first")
    assert head is not None
    assert repository.read_blob(head, name) == b"{}"


def test_rejected_ref_keeps_the_stashed_commit_findable(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    (tmp_path / ".guildbotics" / "state" / "a.json").write_text('{"v": 1}')
    repository.stage(["state/a.json"])
    commit = repository.commit("first")
    assert commit is not None

    ref = repository.save_rejected("rejection-1", commit)

    assert ref == f"{REJECTED_REF_PREFIX}/rejection-1"
    assert repository.read_blob(ref, "state/a.json") == b'{"v": 1}'
    assert repository.rejected_id_for(commit) == "rejection-1"


def test_rejected_id_is_absent_for_an_unstashed_commit(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    (tmp_path / ".guildbotics" / "state" / "a.json").write_text("{}")
    repository.stage(["state/a.json"])
    commit = repository.commit("first")
    assert commit is not None

    assert repository.rejected_id_for(commit) is None


def test_restore_from_index_reinstates_and_removes(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    kept = tmp_path / ".guildbotics" / "state" / "kept.json"
    kept.write_text('{"v": 1}')
    repository.stage(["state/kept.json"])
    repository.commit("first")

    kept.write_text('{"v": 2}')
    added = tmp_path / ".guildbotics" / "state" / "added.json"
    added.write_text("{}")

    repository.restore_from_index(["state/kept.json", "state/added.json"])

    assert kept.read_text() == '{"v": 1}'
    assert not added.exists()


def test_ahead_behind_counts_each_side(tmp_path: Path) -> None:
    repository = _workspace(tmp_path)
    state = tmp_path / ".guildbotics" / "state"
    (state / "a.json").write_text("{}")
    repository.stage(["state/a.json"])
    first = repository.commit("first")
    (state / "b.json").write_text("{}")
    repository.stage(["state/b.json"])
    second = repository.commit("second")
    assert first is not None and second is not None

    assert repository.ahead_behind(second, first) == (1, 0)
    assert repository.ahead_behind(first, second) == (0, 1)


def test_remote_git_kills_a_command_that_never_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished hub can hang name resolution forever; the wall clock ends it."""
    repository = _workspace(tmp_path)
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", sys.executable)

    started = time.monotonic()
    with pytest.raises(HubTimeoutError):
        _run_remote_git(
            repository._repo(),
            "-c",
            "import time; time.sleep(60)",
            timeout=0.5,
        )
    assert time.monotonic() - started < 10


def test_remote_git_returns_output_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _workspace(tmp_path)
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", sys.executable)

    output = _run_remote_git(repository._repo(), "-c", "print('answered')")
    assert output.strip() == "answered"

    with pytest.raises(GitCommandError) as excinfo:
        _run_remote_git(
            repository._repo(),
            "-c",
            "import sys; sys.stderr.write('non-fast-forward'); sys.exit(1)",
        )
    assert "non-fast-forward" in str(excinfo.value)
