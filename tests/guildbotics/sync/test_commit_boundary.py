"""What the commit boundary validates is what it commits, byte for byte.

Reading a file to check it and then letting ``git add`` read it again are two
reads of something a writer can change in between, and the second read is what
becomes shared history. The devices that receive the result stop their queues
on it, while the device that sent it stays green -- its working tree matches
the commit it made, so it never looks at that file again.

Writers hold the shared-write lock now, which is what keeps them out of this
window. These tests are about the other half: that the window is not there to
be raced, so a writer that somehow does get in cannot put unchecked content
into the history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.sync.commits import commit_shared_changes
from guildbotics.sync.local_repository import LocalSyncRepository
from tests.guildbotics.sync.conftest import Device

OVERSIZED = b"x" * (1_048_576 + 1)


def _commit(device: Device) -> tuple[list[str], list[str]]:
    outcome = commit_shared_changes(device.repository, device_id="device-mac")
    return (
        [item.path for item in outcome.unsendable],
        _committed_paths(device.repository),
    )


def _committed_paths(repository: LocalSyncRepository) -> list[str]:
    head = repository.head()
    assert head is not None
    output = repository._repo().git.ls_tree("-r", "--name-only", head)
    return sorted(path for path in output.splitlines() if path)


def test_a_file_that_does_not_validate_is_held_back_and_left_on_disk(
    first: Device,
) -> None:
    """The user's work stays where they can fix it; only sharing waits."""
    first.write("config/team/project.yml", "language: ja\n")
    first.write_bytes("state/too-big.json", OVERSIZED)

    held, committed = _commit(first)

    assert held == ["state/too-big.json"]
    assert "config/team/project.yml" in committed
    assert "state/too-big.json" not in committed
    assert (first.shared / "state/too-big.json").read_bytes() == OVERSIZED


def test_content_that_appears_after_validation_is_not_committed_unchecked(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race this boundary used to have, driven directly.

    A writer that slips in between the check and the staging is simulated by
    replacing the file the moment the boundary reads it. Staging before reading
    means what is read is a snapshot in the index, so the later content is
    simply not in this commit -- rather than being in it, unchecked.
    """
    first.write("state/journal.jsonl", '{"schema_version": 1}\n')
    path = first.shared / "state/journal.jsonl"
    original = LocalSyncRepository.read_staged

    def replace_then_read(self: LocalSyncRepository, relative: str) -> bytes | None:
        path.write_bytes(OVERSIZED)
        return original(self, relative)

    monkeypatch.setattr(LocalSyncRepository, "read_staged", replace_then_read)

    held, committed = _commit(first)

    assert held == []
    assert "state/journal.jsonl" in committed
    assert first.repository.read_blob("HEAD", "state/journal.jsonl") == (
        b'{"schema_version": 1}\n'
    )


def test_a_deletion_that_is_recreated_is_checked_as_content(first: Device) -> None:
    """A deletion needs no check; what replaced it is not a deletion.

    The boundary used to decide "this is a deletion, skip validation" from the
    working tree and then stage whatever was there, so recreating the file in
    between put content into the history that nothing had looked at.
    """
    first.write("state/thing.json", '{"schema_version": 1}\n')
    commit_shared_changes(first.repository, device_id="device-mac")
    (first.shared / "state/thing.json").unlink()
    first.write_bytes("state/thing.json", OVERSIZED)

    held, committed = _commit(first)

    assert held == ["state/thing.json"]
    assert first.repository.read_blob("HEAD", "state/thing.json") == (
        b'{"schema_version": 1}\n'
    )
    assert "state/thing.json" in committed


def test_a_held_back_file_does_not_block_the_rest_of_the_same_pass(
    first: Device,
) -> None:
    """One unshareable file is not a reason to stop sharing anything else."""
    first.write("config/team/project.yml", "language: ja\n")
    first.write("state/other.json", "{}\n")
    first.write_bytes("state/too-big.json", OVERSIZED)

    held, committed = _commit(first)

    assert held == ["state/too-big.json"]
    assert "config/team/project.yml" in committed
    assert "state/other.json" in committed


def test_an_in_progress_atomic_write_is_not_part_of_the_shared_set(
    first: Device,
) -> None:
    """An atomic write leaves its temporary file inside the shared tree.

    It exists for the moment between writing and renaming, which is long
    enough to be enumerated. Committing it adds a junk path to every device's
    history, and its disappearance before ``git add`` fails the cycle -- which
    is then reported as a hub this device could not reach.
    """
    first.write("state/thing.json", "{}\n")
    (first.shared / "state/thing.json.abc123.tmp").write_bytes(b"half written")

    changed = [change.path for change in first.repository.working_tree_changes()]

    assert changed == ["state/thing.json"]


def test_nothing_to_commit_leaves_the_head_alone(first: Device) -> None:
    """A cycle with no local change is not a cycle that makes an empty commit."""
    before = first.repository.head()

    held, _ = _commit(first)

    assert held == []
    assert first.repository.head() == before


def test_the_working_tree_is_left_staged_only_with_what_was_committed(
    first: Device,
) -> None:
    """A held-back file must not stay in the index after the pass.

    Left staged, it would be swept into the next commit that happens for any
    other reason -- still without ever having been checked.
    """
    first.write("state/too-big.json", "{}\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    (first.shared / "state/too-big.json").write_bytes(OVERSIZED)

    commit_shared_changes(first.repository, device_id="device-mac")
    staged = first.repository._repo().git.diff("--cached", "--name-only")

    assert staged == ""


def test_a_recreated_deletion_that_validates_is_committed_as_content(
    first: Device, tmp_path: Path
) -> None:
    """The other side of the deletion case: valid content still gets through."""
    first.write("state/thing.json", '{"schema_version": 1}\n')
    commit_shared_changes(first.repository, device_id="device-mac")
    (first.shared / "state/thing.json").unlink()
    first.write("state/thing.json", '{"schema_version": 1, "again": true}\n')

    held, _ = _commit(first)

    assert held == []
    assert first.repository.read_blob("HEAD", "state/thing.json") == (
        b'{"schema_version": 1, "again": true}\n'
    )
