from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
import yaml  # type: ignore

from guildbotics.utils import workspace_sync_port
from guildbotics.workspace.config_repository import (
    ConfigRepository,
    StaleConfigWriteError,
    blob_id,
)
from guildbotics.workspace.validation import SharedFileInvalidError
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort

PROJECT = "team/project.yml"


@pytest.fixture
def repository() -> ConfigRepository:
    return ConfigRepository()


def project_yaml(name: str) -> str:
    return yaml.safe_dump({"name": name})


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
    repository: ConfigRepository, port: RecordingPort
) -> None:
    content = project_yaml("GuildBotics")
    repository.write_config(PROJECT, None, content)

    snapshot = repository.read_config(PROJECT)

    assert snapshot is not None
    assert snapshot.content == content
    assert snapshot.blob_id == blob_id(content.encode())
    assert snapshot.relative_path == "config/team/project.yml"
    assert snapshot.path.read_text(encoding="utf-8") == content


def test_creating_a_config_expects_no_prior_revision(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    written = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))

    assert written.content == project_yaml("GuildBotics")
    assert [change.paths for change in port.changes] == [("config/team/project.yml",)]


def test_creating_over_an_existing_config_is_refused(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    repository.write_config(PROJECT, None, project_yaml("GuildBotics"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write_config(PROJECT, None, project_yaml("Other"))

    assert error.value.snapshot is not None
    assert error.value.snapshot.content == project_yaml("GuildBotics")
    assert port.changes == []


def test_a_write_at_the_current_revision_succeeds(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    first = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))
    port.changes.clear()

    second = repository.write_config(PROJECT, first.blob_id, project_yaml("Renamed"))

    assert second.content == project_yaml("Renamed")
    assert second.blob_id != first.blob_id
    assert [change.operation for change in port.changes] == ["update"]


def test_a_write_at_a_stale_revision_changes_nothing(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    stale = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))
    repository.write_config(PROJECT, stale.blob_id, project_yaml("Newer"))
    port.changes.clear()

    with pytest.raises(StaleConfigWriteError) as error:
        repository.write_config(PROJECT, stale.blob_id, project_yaml("Older screen"))

    current = repository.read_config(PROJECT)
    assert current is not None
    assert current.content == project_yaml("Newer")
    assert error.value.snapshot == current
    assert port.changes == []


def test_a_write_of_invalid_content_changes_nothing(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    written = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))
    port.changes.clear()

    with pytest.raises(SharedFileInvalidError):
        repository.write_config(PROJECT, written.blob_id, "name: [unclosed")

    current = repository.read_config(PROJECT)
    assert current is not None
    assert current.content == project_yaml("GuildBotics")
    assert port.changes == []


def test_only_one_of_two_concurrent_writers_wins(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    base = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))
    port.changes.clear()
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def write(name: str) -> None:
        writer = ConfigRepository()
        start.wait()
        try:
            writer.write_config(PROJECT, base.blob_id, project_yaml(name))
        except StaleConfigWriteError:
            outcomes.append(f"stale:{name}")
        else:
            outcomes.append(f"written:{name}")

    threads = [threading.Thread(target=write, args=(name,)) for name in ("A", "B")]
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


def test_the_write_lock_is_released_after_each_write(
    repository: ConfigRepository, port: RecordingPort
) -> None:
    written = repository.write_config(PROJECT, None, project_yaml("GuildBotics"))

    # A second repository instance in the same process must not deadlock on the
    # lock the first one already released.
    again = ConfigRepository().write_config(
        PROJECT, written.blob_id, project_yaml("Renamed")
    )

    assert again.content == project_yaml("Renamed")
