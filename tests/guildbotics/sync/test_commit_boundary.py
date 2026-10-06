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
from git import Repo

from guildbotics.sync.commits import (
    UnsendableChange,
    commit_shared_changes,
    held_covers,
)
from guildbotics.sync.local_repository import LocalSyncRepository, SharedEntry
from tests.guildbotics.sync.conftest import Device

OVERSIZED = b"x" * (1_048_576 + 1)


def _commit(device: Device) -> tuple[list[str], list[str]]:
    outcome = commit_shared_changes(device.repository, device_id="device-mac")
    return (
        [item.path for item in outcome.unsendable],
        _committed_paths(device.repository),
    )


def _committed(device: Device, path: str) -> SharedEntry:
    return device.repository.read_entries("HEAD", [path])[path]


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

    def replace_then_read(
        self: LocalSyncRepository, paths: list[str]
    ) -> dict[str, SharedEntry]:
        path.write_bytes(OVERSIZED)
        return original(self, paths)

    monkeypatch.setattr(LocalSyncRepository, "read_staged", replace_then_read)

    held, committed = _commit(first)

    assert held == []
    assert "state/journal.jsonl" in committed
    assert _committed(first, "state/journal.jsonl").data == (b'{"schema_version": 1}\n')


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
    assert _committed(first, "state/thing.json").data == (b'{"schema_version": 1}\n')
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

    changed = first.repository.stage_changes()

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
    assert _committed(first, "state/thing.json").data == (
        b'{"schema_version": 1, "again": true}\n'
    )


# -- Only regular files are shared --------------------------------------------


@pytest.mark.usefixtures("symlinks")
@pytest.mark.parametrize("points_at", ["file", "directory"])
def test_a_link_is_held_back_and_left_on_disk(
    first: Device, tmp_path: Path, points_at: str
) -> None:
    """A link validated by its content would pass: that content is a path.

    Sent, it lands on every other device pointing at whatever that path names
    there.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("outside\n")
    target = outside / "notes.md" if points_at == "file" else outside
    (first.shared / "config/commands").symlink_to(target)
    first.write("config/team/project.yml", "language: ja\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is not a regular file (Git mode 120000)")
    ]
    assert "config/commands" not in _committed_paths(first.repository)
    assert "config/team/project.yml" in _committed_paths(first.repository)
    assert (first.shared / "config/commands").is_symlink()


def test_an_embedded_repository_is_held_back_under_its_own_name(
    first: Device,
) -> None:
    """Git lists an embedded repository as ``name/`` and stages it as a gitlink.

    Read under the listed name, the staged entry looked absent, so the gitlink
    was committed as if it were a deletion. Under its own name it is held, and
    what lies beneath it is covered by that.
    """
    nested = first.shared / "config/nested"
    nested.mkdir()
    (nested / "build.md").write_text("build\n")
    repository = Repo.init(nested)
    repository.index.add(["build.md"])
    repository.index.commit("cloned elsewhere")

    held, committed = _commit(first)

    assert held == ["config/nested"]
    assert "config/nested" not in committed


def test_an_executable_file_is_shared_with_its_mode(
    first: Device, posix_permissions: None
) -> None:
    first.write("config/commands/build.sh", "echo ok\n")
    (first.shared / "config/commands/build.sh").chmod(0o755)

    held, _ = _commit(first)

    assert held == []
    assert _committed(first, "config/commands/build.sh") == (
        SharedEntry(mode="100755", data=b"echo ok\n")
    )


@pytest.mark.usefixtures("symlinks")
def test_a_change_beneath_a_held_link_is_not_shared_either(
    first: Device, tmp_path: Path
) -> None:
    """Replacing a shared directory with a link deletes what it held, in Git's
    eyes. Those deletions are not the user's intent to share -- the files now
    live wherever the link points -- so they wait with the link."""
    first.write("config/commands/build.md", "build\n")
    first.write("config/commandsfile.md", "kept apart\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    (first.shared / "config/commands/build.md").unlink()
    (first.shared / "config/commands").rmdir()
    (tmp_path / "outside").mkdir()
    (first.shared / "config/commands").symlink_to(tmp_path / "outside")
    (first.shared / "config/commandsfile.md").unlink()

    held, committed = _commit(first)

    assert held == ["config/commands"]
    assert "config/commands/build.md" in committed
    # Covered by element, not by prefix: a sibling sharing the prefix travels.
    assert "config/commandsfile.md" not in committed
    assert first.repository._repo().git.diff("--cached", "--name-only") == ""


def test_a_held_name_with_pattern_characters_holds_only_itself(
    first: Device,
) -> None:
    """As a pattern, ``a[b].json`` also matches ``ab.json``; unstaging the held
    file would then quietly drop its valid neighbour from the commit."""
    first.write("state/ab.json", "{}\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    first.write("state/ab.json", '{"edited": true}\n')
    first.write("state/a[b].json", "{not json}")

    held, _ = _commit(first)

    assert held == ["state/a[b].json"]
    assert _committed(first, "state/ab.json").data == b'{"edited": true}\n'


@pytest.mark.parametrize(
    ("path", "covered"),
    [
        ("config/foo", True),
        ("config/foo/bar.md", True),
        ("config/foo/deeper/bar.md", True),
        ("config/foobar.md", False),
        ("config/fo", False),
        ("config", False),
    ],
)
def test_holding_a_path_covers_it_and_what_lies_beneath_it(
    path: str, covered: bool
) -> None:
    held = [UnsendableChange(path="config/foo", reason="is not a regular file")]

    assert held_covers(held, path) is covered
