from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
import yaml  # type: ignore

from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.fileio import save_yaml_file
from guildbotics.workspace.config_repository import (
    ConfigRepository,
    StaleConfigWriteError,
    blob_id,
)
from guildbotics.utils.shared_write_lock import (
    shared_write_lock,
    shared_write_lock_path,
)
from guildbotics.workspace.validation import SharedFileInvalidError
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort

PROJECT = "team/project.yml"
MODEL_MAPPING = "intelligences/model_mapping.yml"


def refuse_to_run() -> None:
    """A write body that must never be reached."""
    pytest.fail("the write must not run against superseded content")


def shared_write_lock_is_held(workspace_root: Path | None = None) -> bool:
    """Whether something else already holds a workspace's shared-write lock.

    Taking the lock a second time is what tells us, because the lock is what
    makes a config save and a synchronization checkout wait for each other.
    """
    try:
        with held_lock(shared_write_lock_path(workspace_root), timeout=0.0):
            return False
    except LockTimeoutError:
        return True


@pytest.fixture
def repository() -> ConfigRepository:
    return ConfigRepository()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / ".guildbotics/config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_yaml(name: str) -> str:
    return yaml.safe_dump({"name": name})


def write(config_dir: Path, relative_path: str, content: str) -> None:
    """Write a config file the way the config screens do.

    Including the span: a screen's save reaches ``save_yaml_file`` from inside
    :meth:`ConfigRepository.write`, which holds the workspace's shared-write
    lock. Where this stands in for a save that already happened, the span is
    simply the write itself.
    """
    with shared_write_lock():
        save_yaml_file(config_dir / relative_path, yaml.safe_load(content))


def test_blob_id_matches_git(tmp_path: Path) -> None:
    data = "name: GuildBotics\n".encode()
    path = tmp_path / "sample"
    path.write_bytes(data)

    expected = subprocess.run(
        ["git", "hash-object", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert blob_id(data) == expected


def test_reading_an_absent_config_returns_none(repository: ConfigRepository) -> None:
    assert repository.read_config(PROJECT) is None


def test_a_read_returns_the_content_and_its_revision(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    content = project_yaml("GuildBotics")
    write(config_dir, PROJECT, content)

    snapshot = repository.read_config(PROJECT)

    assert snapshot is not None
    assert snapshot.content == content
    assert snapshot.blob_id == blob_id(content.encode())
    assert snapshot.relative_path == "config/team/project.yml"
    assert snapshot.path.read_text(encoding="utf-8") == content


def test_a_file_that_does_not_exist_has_an_empty_revision(
    repository: ConfigRepository, config_dir: Path
) -> None:
    """Absence is a revision of its own, so a file that appears afterwards is a
    change the guard can still see."""
    write(config_dir, PROJECT, project_yaml("GuildBotics"))

    revisions = repository.revisions([PROJECT, MODEL_MAPPING])

    assert revisions[PROJECT] == blob_id(project_yaml("GuildBotics").encode())
    assert revisions[MODEL_MAPPING] == ""


def test_a_file_created_since_the_read_refuses_the_save(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    expected = repository.revisions([PROJECT, MODEL_MAPPING])
    write(config_dir, MODEL_MAPPING, project_yaml("gpt"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError):
        repository.write(refuse_to_run, expected=expected)

    assert port.changes == []


def test_a_guarded_write_at_the_current_revisions_applies(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    expected = repository.revisions([PROJECT])
    port.changes.clear()

    receipt = repository.write(
        lambda: write(config_dir, PROJECT, project_yaml("Renamed")),
        expected=expected,
        report=lambda: repository.revisions([PROJECT]),
    )

    snapshot = repository.read_config(PROJECT)
    assert snapshot is not None
    assert snapshot.content == project_yaml("Renamed")
    # The receipt describes what this write left, not what it replaced.
    assert receipt.revisions == {PROJECT: blob_id(project_yaml("Renamed").encode())}
    assert [change.operation for change in port.changes] == ["update"]


def test_a_guarded_write_at_a_stale_revision_changes_nothing(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    stale = repository.revisions([PROJECT])
    write(config_dir, PROJECT, project_yaml("Newer"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write(refuse_to_run, expected=stale)

    current = repository.read_config(PROJECT)
    assert current is not None
    assert current.content == project_yaml("Newer")
    assert error.value.relative_path == "config/team/project.yml"
    # Read under the same lock as the comparison, so the screen reloads with
    # what the comparison actually saw.
    assert error.value.revisions == {PROJECT: current.blob_id}
    assert port.changes == []


def test_a_guarded_write_expecting_absence_is_refused_once_the_file_exists(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write(refuse_to_run, expected={PROJECT: ""})

    assert error.value.revisions[PROJECT] != ""
    assert port.changes == []


def test_one_stale_file_refuses_the_whole_save(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    """A save spanning several files applies completely or not at all."""
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    write(config_dir, MODEL_MAPPING, project_yaml("gpt"))
    expected = repository.revisions([PROJECT, MODEL_MAPPING])
    # Another device's change arrives for one of the two files.
    write(config_dir, MODEL_MAPPING, project_yaml("claude"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write(refuse_to_run, expected=expected)

    assert error.value.relative_path == "config/intelligences/model_mapping.yml"
    untouched = repository.read_config(PROJECT)
    assert untouched is not None
    assert untouched.content == project_yaml("GuildBotics")
    assert port.changes == []


def test_tree_revisions_cover_every_file_under_the_directory(
    repository: ConfigRepository, config_dir: Path
) -> None:
    write(config_dir, MODEL_MAPPING, project_yaml("gpt"))
    write(config_dir, "intelligences/models/openai.yml", project_yaml("openai"))
    write(config_dir, PROJECT, project_yaml("GuildBotics"))

    revisions = repository.tree_revisions("intelligences")

    assert set(revisions) == {
        "intelligences/",
        MODEL_MAPPING,
        "intelligences/models/openai.yml",
    }


def test_tree_revisions_of_an_absent_directory_still_state_it_is_absent(
    repository: ConfigRepository,
) -> None:
    """An empty mapping would be read as "nothing to compare" and skip the check."""
    revisions = repository.tree_revisions("intelligences")

    assert set(revisions) == {"intelligences/"}
    assert revisions["intelligences/"] != ""


def test_a_file_another_device_added_refuses_a_directory_save(
    repository: ConfigRepository, config_dir: Path
) -> None:
    """The screen prunes what it did not read, so an addition it never saw is stale."""
    write(config_dir, MODEL_MAPPING, project_yaml("gpt"))
    expected = repository.tree_revisions("intelligences")
    write(config_dir, "intelligences/cli_agent_mapping.yml", project_yaml("codex"))

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write(refuse_to_run, expected=expected)

    assert error.value.relative_path == "config/intelligences"


def test_a_directory_another_device_created_refuses_a_save(
    repository: ConfigRepository, config_dir: Path
) -> None:
    """A member inheriting the team defaults has no files, and still has a position."""
    scope = "team/members/aiko/intelligences"
    expected = repository.tree_revisions(scope)
    write(config_dir, f"{scope}/cli_agent_mapping.yml", project_yaml("codex"))

    with pytest.raises(StaleConfigWriteError):
        repository.write(refuse_to_run, expected=expected)


def test_a_guarded_save_holds_the_workspace_shared_write_lock(
    repository: ConfigRepository, config_dir: Path
) -> None:
    """Synchronization must not check content out between the compare and the write."""
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    expected = repository.revisions([PROJECT])

    held: list[bool] = []
    receipt = repository.write(
        lambda: held.append(shared_write_lock_is_held()),
        expected=expected,
        # The report runs inside the lock too: a revision read after it was
        # released could describe content this write never made.
        report=lambda: {"held-while-reporting": str(shared_write_lock_is_held())},
    )

    assert held == [True]
    assert receipt.revisions == {"held-while-reporting": "True"}
    assert not shared_write_lock_is_held()


def test_a_directory_revision_ignores_the_content_of_its_files(
    repository: ConfigRepository, config_dir: Path
) -> None:
    """Content is covered file by file, so a save's own earlier step is not a conflict."""
    write(config_dir, MODEL_MAPPING, project_yaml("gpt"))
    before = repository.tree_revisions("intelligences")["intelligences/"]

    write(config_dir, MODEL_MAPPING, project_yaml("claude"))

    assert repository.tree_revisions("intelligences")["intelligences/"] == before


def test_only_one_of_two_concurrent_savers_wins(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    write(config_dir, PROJECT, project_yaml("GuildBotics"))
    expected = repository.revisions([PROJECT])
    port.changes.clear()
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def save(name: str) -> None:
        saver = ConfigRepository()
        start.wait()
        try:
            saver.write(
                lambda: write(config_dir, PROJECT, project_yaml(name)),
                expected=expected,
            )
        except StaleConfigWriteError:
            outcomes.append(f"stale:{name}")
        else:
            outcomes.append(f"written:{name}")

    threads = [threading.Thread(target=save, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome.split(":")[0] for outcome in outcomes) == ["stale", "written"]
    assert len(port.changes) == 1
    winner = next(name for name in ("A", "B") if f"written:{name}" in outcomes)
    current = repository.read_config(PROJECT)
    assert current is not None
    assert current.content == project_yaml(winner)


@pytest.mark.parametrize(
    "relative_path",
    [
        "../state/workspace.json",
        "team/../../state/workspace.json",
        "/etc/passwd",
        "",
    ],
)
def test_a_path_outside_the_config_directory_is_refused(
    repository: ConfigRepository,
    port: RecordingPort,
    tmp_path: Path,
    relative_path: str,
) -> None:
    with pytest.raises(SharedFileInvalidError):
        repository.write(refuse_to_run, expected={relative_path: ""})
    with pytest.raises(SharedFileInvalidError):
        repository.read_config(relative_path)

    assert not (tmp_path / ".guildbotics/state/workspace.json").exists()
    assert port.changes == []


def test_a_symlink_out_of_the_config_directory_is_refused(
    repository: ConfigRepository, port: RecordingPort, tmp_path: Path
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "escape").symlink_to(tmp_path / ".guildbotics/state")

    with pytest.raises(SharedFileInvalidError):
        repository.write(refuse_to_run, expected={"escape/workspace.json": ""})

    assert port.changes == []


def test_the_write_lock_is_released_after_each_save(
    repository: ConfigRepository, config_dir: Path, port: RecordingPort
) -> None:
    repository.write(lambda: write(config_dir, PROJECT, project_yaml("GuildBotics")))

    # A second repository instance in the same process must not deadlock on the
    # lock the first one already released.
    ConfigRepository().write(
        lambda: write(config_dir, PROJECT, project_yaml("Renamed")),
        expected=repository.revisions([PROJECT]),
    )

    snapshot = repository.read_config(PROJECT)
    assert snapshot is not None
    assert snapshot.content == project_yaml("Renamed")
