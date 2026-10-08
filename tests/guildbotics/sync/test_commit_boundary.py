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

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from git import GitCommandError, Repo

import guildbotics.sync.local_repository as local_repository
from guildbotics.sync.commits import (
    UnsendableChange,
    commit_shared_changes,
    held_covers,
)
from guildbotics.sync.local_repository import (
    LocalSyncRepository,
    SharedEntry,
    SyncRepositoryError,
)
from tests.guildbotics.sync.conftest import Device, refuse_listing

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

    assert changed.paths == ("state/thing.json",)
    assert changed.refused == ()


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


def _init_repository(path: Path) -> Repo:
    path.mkdir()
    (path / "note.md").write_text("note\n")
    return Repo.init(path)


def test_a_repository_with_no_commit_is_held_and_its_root_is_still_sent(
    first: Device,
) -> None:
    """``git init`` with no commit makes ``git add`` refuse that directory.

    The refusal used to abort the add, so nothing else in the root was sent
    and the cycle was reported as a hub this device could not reach. The
    directory waits with a reason, and the file beside it is committed.
    """
    _init_repository(first.shared / "config/commands")
    first.write("config/team/project.yml", "language: ja\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is a Git repository with no commit")
    ]
    assert "config/team/project.yml" in _committed_paths(first.repository)
    assert "config/commands" not in _committed_paths(first.repository)
    assert "config/commands/note.md" not in _committed_paths(first.repository)
    assert (first.shared / "config/commands/note.md").read_text() == "note\n"
    assert first.repository._repo().git.diff("--cached", "--name-only") == ""


def test_every_repository_with_no_commit_is_held(first: Device) -> None:
    """One refused directory is not the only one, and another root still goes."""
    _init_repository(first.shared / "config/commands")
    _init_repository(first.shared / "config/other")
    first.write("state/kept.json", "{}\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is a Git repository with no commit"),
        ("config/other", "is a Git repository with no commit"),
    ]
    assert "state/kept.json" in _committed_paths(first.repository)


def test_a_repository_whose_name_is_a_pattern_is_held_alone(first: Device) -> None:
    """``a[b]`` is a name, not a pattern that also matches ``ab``."""
    _init_repository(first.shared / "config/a[b]")
    first.write("config/ab.yml", "kept: true\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [item.path for item in outcome.unsendable] == ["config/a[b]"]
    assert "config/ab.yml" in _committed_paths(first.repository)


def test_a_repository_path_with_a_space_is_held_under_that_name(first: Device) -> None:
    _init_repository(first.shared / "config/my commands")
    first.write("config/team/project.yml", "language: ja\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [item.path for item in outcome.unsendable] == ["config/my commands"]
    assert "config/team/project.yml" in _committed_paths(first.repository)


def test_a_commit_in_a_nested_repository_holds_it_as_a_gitlink(first: Device) -> None:
    """The same directory, once it has a commit, is the gitlink #723 holds."""
    nested = _init_repository(first.shared / "config/commands")
    commit_shared_changes(first.repository, device_id="device-mac")
    nested.index.add(["note.md"])
    nested.index.commit("commands")
    first.write("config/team/project.yml", "language: ja\n")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is not a regular file (Git mode 160000)")
    ]
    assert "config/team/project.yml" in _committed_paths(first.repository)
    assert "config/commands" not in _committed_paths(first.repository)


@pytest.mark.parametrize(
    ("committed", "reason"),
    [
        (False, "is a Git repository with no commit"),
        (True, "is not a regular file (Git mode 160000)"),
    ],
)
def test_an_unlistable_directory_inside_a_held_repository_stops_nothing_else(
    first: Device, monkeypatch: pytest.MonkeyPatch, committed: bool, reason: str
) -> None:
    """Git does not walk into an embedded repository, so neither does the check.

    The repository is held as a whole. A directory inside it that cannot be
    listed is not a shared directory, and failing on it would keep the file
    beside the repository from being sent.
    """
    commands = first.shared / "config/commands"
    nested = _init_repository(commands)
    (commands / "private").mkdir()
    (commands / "private/secret.md").write_text("secret\n")
    if committed:
        nested.index.add(["note.md"])
        nested.index.commit("commands")
    first.write("config/sent.md", "sent\n")
    refuse_listing(monkeypatch, commands / "private")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", reason)
    ]
    assert "config/sent.md" in _committed_paths(first.repository)


def test_an_unlistable_directory_inside_an_ignored_one_stops_nothing_else(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git does not walk into a directory an ignore rule matches, so neither does the check.

    The rule here is one the user may have outside the workspace's own
    ``.gitignore``, like a global ``node_modules/``. The ignored directory is
    also a repository with no commit: being ignored, it is not a change, so
    it is not held either.
    """
    exclude = Path(first.repository._repo().git_dir) / "info" / "exclude"
    exclude.write_text("node_modules/\n")
    (first.shared / "config/commands").mkdir()
    modules = first.shared / "config/commands/node_modules"
    _init_repository(modules)
    (modules / "private").mkdir()
    (modules / "private/package.js").write_text("module\n")
    first.write("config/sent.md", "sent\n")
    refuse_listing(monkeypatch, modules / "private")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert outcome.unsendable == ()
    assert "config/sent.md" in _committed_paths(first.repository)
    assert "config/commands/node_modules/private/package.js" not in (
        _committed_paths(first.repository)
    )


def test_an_ignored_directory_holding_tracked_files_is_still_checked(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git walks an ignored directory that holds tracked files, so the check does too.

    Its tracked file could be staged as deleted, so the pass fails rather
    than sending anything.
    """
    first.write("config/vendor/kept.md", "kept\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    exclude = Path(first.repository._repo().git_dir) / "info" / "exclude"
    exclude.write_text("vendor/\n")
    first.write("config/sent.md", "sent\n")
    head = first.repository.head()
    refuse_listing(monkeypatch, first.shared / "config/vendor")

    with pytest.raises(SyncRepositoryError, match="vendor"):
        commit_shared_changes(first.repository, device_id="device-mac")

    assert first.repository.head() == head
    assert "config/vendor/kept.md" in _committed_paths(first.repository)


def _race_the_status_before_add(
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[], None],
    restore: Callable[[], None],
) -> None:
    """Change the tree for the first status of this pass, and put it back before add.

    ``stage_changes`` reads status, then adds. A path that exists only for that
    read used to be reported as refused. ``change`` and ``restore`` are called
    with no arguments.
    """
    original = local_repository._changed_paths
    calls = 0

    def wrapped(repository: Repo) -> tuple[list[str], set[str]]:
        nonlocal calls
        calls += 1
        if calls != 1:
            return original(repository)
        change()
        try:
            return original(repository)
        finally:
            restore()

    monkeypatch.setattr(local_repository, "_changed_paths", wrapped)


def test_a_file_that_vanishes_between_status_and_add_is_not_refused(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor scratch file can disappear after status and before add.

    It was never skipped by ``git add``. Reporting it as refused holds the
    path, and a later restore then leaves the previous bytes in place.
    """
    scratch = first.shared / "state/scratch.json"
    first.write("state/kept.json", "{}\n")

    def appear() -> None:
        scratch.write_text("{}\n")

    def disappear() -> None:
        scratch.unlink()

    _race_the_status_before_add(monkeypatch, appear, disappear)
    changed = first.repository.stage_changes()

    assert changed.refused == ()
    assert changed.paths == ("state/kept.json",)


def test_a_tracked_file_put_back_before_add_is_not_refused(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracked file edited and then restored to HEAD is not a skipped path.

    Add exits 0: the file matches HEAD again, so there is nothing to stage
    and nothing to hold.
    """
    first.write("config/team/project.yml", "language: en\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    path = first.shared / "config/team/project.yml"

    def edit() -> None:
        path.write_bytes(b"language: fr\n")

    def restore() -> None:
        path.write_bytes(b"language: en\n")

    _race_the_status_before_add(monkeypatch, edit, restore)
    changed = first.repository.stage_changes()

    assert changed.refused == ()
    assert changed.paths == ()


def test_a_file_that_vanishes_before_add_is_not_unsendable(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit boundary must not hold a path add never refused."""
    scratch = first.shared / "config/scratch.yml"
    first.write("state/kept.json", "{}\n")

    def appear() -> None:
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text("temporary: true\n")

    def disappear() -> None:
        scratch.unlink()

    _race_the_status_before_add(monkeypatch, appear, disappear)
    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert outcome.unsendable == ()
    assert "state/kept.json" in _committed_paths(first.repository)
    assert not scratch.exists()


def test_a_tracked_file_put_back_before_add_is_not_unsendable(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring HEAD content before add leaves that content, and no hold."""
    first.write("config/team/project.yml", "language: en\n")
    commit_shared_changes(first.repository, device_id="device-mac")
    path = first.shared / "config/team/project.yml"
    first.write("state/kept.json", "{}\n")

    def edit() -> None:
        path.write_bytes(b"language: fr\n")

    def restore() -> None:
        path.write_bytes(b"language: en\n")

    _race_the_status_before_add(monkeypatch, edit, restore)
    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert outcome.unsendable == ()
    assert _committed(first, "config/team/project.yml").data == b"language: en\n"
    assert "state/kept.json" in _committed_paths(first.repository)


def test_an_excluded_repository_is_still_held_when_another_path_vanishes(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold names the repository status found, not the path that vanished."""
    _init_repository(first.shared / "config/commands")
    scratch = first.shared / "state/scratch.json"
    first.write("config/team/project.yml", "language: ja\n")

    def appear() -> None:
        scratch.write_text("{}\n")

    def disappear() -> None:
        scratch.unlink()

    _race_the_status_before_add(monkeypatch, appear, disappear)
    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is a Git repository with no commit")
    ]
    assert "config/team/project.yml" in _committed_paths(first.repository)
    assert "state/scratch.json" not in _committed_paths(first.repository)


def test_a_repository_removed_after_status_is_not_held(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository status listed can be gone before it is classified.

    Nothing remains to hold, and holding the name would skip restoring the
    hub's copy beneath it. The file beside it is still committed, and the pass
    does not fail.
    """
    commands = first.shared / "config/commands"
    _init_repository(commands)
    first.write("config/team/project.yml", "language: ja\n")

    def remove() -> None:
        shutil.rmtree(commands)

    _race_the_status_before_add(monkeypatch, lambda: None, remove)
    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert outcome.unsendable == ()
    assert "config/team/project.yml" in _committed_paths(first.repository)
    assert not commands.exists()


def test_a_directory_that_lost_its_git_metadata_after_status_is_sent(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``.git`` the directory holds ordinary files, and they are sent."""
    commands = first.shared / "config/commands"
    _init_repository(commands)
    first.write("config/team/project.yml", "language: ja\n")

    def drop_metadata() -> None:
        shutil.rmtree(commands / ".git")

    _race_the_status_before_add(monkeypatch, lambda: None, drop_metadata)
    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert outcome.unsendable == ()
    assert "config/commands/note.md" in _committed_paths(first.repository)
    assert "config/team/project.yml" in _committed_paths(first.repository)


def test_a_repository_created_after_status_fails_the_pass_until_the_next_one(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing add is not read for what it skipped.

    The repository appears after status, so nothing excludes it and the add
    fails. Nothing of the pass is committed, and the next pass finds the
    repository and holds it while the file beside it is sent.
    """
    commands = first.shared / "config/commands"
    first.write("config/team/project.yml", "language: ja\n")
    head = first.repository.head()

    def create() -> None:
        _init_repository(commands)

    _race_the_status_before_add(monkeypatch, lambda: None, create)
    with pytest.raises(GitCommandError, match="does not have a commit"):
        commit_shared_changes(first.repository, device_id="device-mac")

    assert first.repository.head() == head
    outcome = commit_shared_changes(first.repository, device_id="device-mac")
    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is a Git repository with no commit")
    ]
    assert "config/team/project.yml" in _committed_paths(first.repository)


def test_a_dirty_gitlink_already_indexed_is_not_refused(
    first: Device,
) -> None:
    """Status keeps listing a gitlink whose commit did not change.

    It is listed by name, without the trailing slash of a repository status
    found untracked, so it is not excluded as one with no commit. Refusing it
    would hold the path and skip restoring whatever the hub has there.
    """
    nested = _init_repository(first.shared / "config/plugin")
    nested.index.add(["note.md"])
    nested.index.commit("plugin")
    first.write("state/kept.json", "{}\n")
    repository = first.repository._repo()
    repository.git.add("--", "config/plugin", "state/kept.json")
    repository.git.commit("--no-verify", "-m", "history that already has the gitlink")
    (first.shared / "config/plugin/note.md").write_text("dirty\n")
    _init_repository(first.shared / "config/commands")

    outcome = commit_shared_changes(first.repository, device_id="device-mac")

    assert [(item.path, item.reason) for item in outcome.unsendable] == [
        ("config/commands", "is a Git repository with no commit")
    ]
    assert "config/plugin" not in [item.path for item in outcome.unsendable]


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
