"""Connecting a workspace to a hub: registering, joining, and taking a copy."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from git import GitCommandError, Repo

from guildbotics.sync import enrollment
from guildbotics.sync.local_repository import REJECTED_REF_PREFIX, LocalSyncRepository
from guildbotics.workspace.identity import (
    ensure_workspace_identity,
    read_workspace_identity,
)
from tests.guildbotics.sync.conftest import Device, make_device

CONFIG = "config/team/project.yml"
ROLES = "config/roles/reviewer.yml"


@pytest.fixture
def rejections() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def recorder(rejections: list[dict[str, Any]]) -> enrollment.RejectionRecorder:
    return lambda **fields: rejections.append(fields)


def _workspace(root: Path, **files: str) -> Path:
    """A workspace directory holding shared content but no repository yet."""
    for relative, text in files.items():
        path = root / ".guildbotics" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _as_machine(tmp_path: Path, name: str) -> Iterator[None]:
    """Run a block as a different machine, with its own device identity.

    Device identity is machine-wide rather than per workspace, so two
    workspaces in one test would otherwise publish the same device record and
    collide over it -- a conflict no pair of real machines can have.
    """
    home = tmp_path / f"machine-{name}"
    home.mkdir(exist_ok=True)
    previous = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write(root: Path, relative: str, text: str) -> None:
    """Write a shared file into an existing workspace."""
    path = root / ".guildbotics" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hub_file(hub: Path, path: str) -> str | None:
    """Read a file from the hub verbatim, trailing newline included."""
    try:
        content = Repo(hub).git.cat_file(
            "blob",
            f"main:{path}",
            stdout_as_string=False,
            strip_newline_in_stdout=False,
        )
    except GitCommandError:
        return None
    return bytes(content).decode("utf-8")


# -- Registering the first content --------------------------------------------


def test_an_empty_hub_receives_this_workspace_as_its_first_content(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})

    result = enrollment.enroll(str(hub), root)

    assert result.mode == "register"
    assert result.rejection_id is None
    assert _hub_file(hub, CONFIG) == "name: demo\n"
    assert _hub_file(hub, "state/workspace.json") is not None


def test_registering_keeps_this_workspace_identifier(tmp_path: Path, hub: Path) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})

    result = enrollment.enroll(str(hub), root)

    identity = read_workspace_identity(root)
    assert identity is not None
    assert result.workspace_id == identity.workspace_id


def test_a_registered_workspace_publishes_this_device(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})

    enrollment.enroll(str(hub), root)

    devices = list((root / ".guildbotics" / "state" / "devices").glob("*.json"))
    assert len(devices) == 1


# -- Taking a copy ------------------------------------------------------------


def test_a_new_machine_takes_the_hub_content_as_its_workspace(
    tmp_path: Path, hub: Path
) -> None:
    source = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})
    registered = enrollment.enroll(str(hub), source)

    workspace_id = enrollment.clone_workspace(str(hub), tmp_path / "windows")

    assert workspace_id == registered.workspace_id
    assert (
        tmp_path / "windows" / ".guildbotics" / CONFIG
    ).read_text() == "name: demo\n"


def test_a_copy_keeps_device_only_content_out_of_the_repository(
    tmp_path: Path, hub: Path
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: d\n"}))

    enrollment.clone_workspace(str(hub), tmp_path / "windows")

    ignore = (tmp_path / "windows" / ".guildbotics" / ".gitignore").read_text()
    assert "local/" in ignore
    assert ".env" in ignore


def test_a_folder_this_device_only_opened_can_still_take_a_copy(
    tmp_path: Path, hub: Path
) -> None:
    """Selecting a folder in the Desktop opens it as a workspace, which writes
    this device's diagnostics under ``local/`` before anything is asked for.
    Treating that as a workspace already present refused every folder the user
    had picked -- the only way the screen offers to name a destination."""
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: d\n"}))
    opened = tmp_path / "windows"
    scratch = opened / ".guildbotics" / "local" / "run"
    scratch.mkdir(parents=True)
    (scratch / "diagnostics.jsonl").write_text("{}\n", encoding="utf-8")

    enrollment.clone_workspace(str(hub), opened)

    assert (opened / ".guildbotics" / CONFIG).read_text() == "name: d\n"
    # The device's own scratch is still there: a copy adds shared content, it
    # does not clear the folder out.
    assert (scratch / "diagnostics.jsonl").is_file()


def test_a_workspace_that_already_exists_is_not_replaced_by_a_copy(
    tmp_path: Path, hub: Path
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: d\n"}))
    existing = _workspace(tmp_path / "windows", **{CONFIG: "name: other\n"})
    LocalSyncRepository(existing).initialize()

    with pytest.raises(Exception):
        enrollment.clone_workspace(str(hub), existing)


# -- Joining with existing content --------------------------------------------


def test_joining_adopts_the_hub_version_of_a_file_both_sides_have(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert result.mode == "join"
    assert (joining / ".guildbotics" / CONFIG).read_text() == "name: hub\n"
    assert CONFIG in result.adopted


def test_joining_keeps_and_shares_a_file_only_this_machine_has(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", ROLES: "a: b\n"}
    )

    enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert (joining / ".guildbotics" / ROLES).read_text() == "a: b\n"
    assert _hub_file(hub, ROLES) == "a: b\n"


def test_joining_adopts_the_hub_workspace_identifier(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    """Two identifiers for one workspace would let a hub mix two of them."""
    registered = enrollment.enroll(
        str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"})
    )
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})
    before = ensure_workspace_identity(joining)

    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert before.workspace_id != registered.workspace_id
    assert result.workspace_id == registered.workspace_id
    identity = read_workspace_identity(joining)
    assert identity is not None
    assert identity.workspace_id == registered.workspace_id


def test_displaced_content_is_kept_on_this_device_and_recorded(
    tmp_path: Path,
    hub: Path,
    recorder: enrollment.RejectionRecorder,
    rejections: list[dict[str, Any]],
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert result.rejection_id is not None
    assert rejections[0]["rejection_id"] == result.rejection_id
    assert CONFIG in rejections[0]["paths"]
    ref = f"{REJECTED_REF_PREFIX}/{result.rejection_id}"
    stashed = Repo(joining / ".guildbotics").git.show(f"{ref}:{CONFIG}")
    assert stashed == "name: mine"


def test_a_rejection_names_the_workspace_the_device_ends_up_in(
    tmp_path: Path,
    hub: Path,
    recorder: enrollment.RejectionRecorder,
    rejections: list[dict[str, Any]],
) -> None:
    registered = enrollment.enroll(
        str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"})
    )
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert rejections[0]["workspace_id"] == registered.workspace_id


def test_nothing_is_displaced_when_the_two_sides_agree(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    source = _workspace(tmp_path / "mac", **{CONFIG: "name: same\n"})
    enrollment.enroll(str(hub), source)
    enrollment.clone_workspace(str(hub), tmp_path / "windows")

    result = enrollment.enroll(
        str(hub), tmp_path / "windows", record_rejection=recorder
    )

    assert result.rejection_id is None
    assert result.adopted == ()


def test_a_hub_without_a_workspace_identity_is_refused(
    tmp_path: Path, hub: Path
) -> None:
    """A repository that is not a GuildBotics workspace must not be adopted."""
    stranger = Repo.init(tmp_path / "stranger", initial_branch="main")
    stranger.git.config("user.name", "someone")
    stranger.git.config("user.email", "someone@example.invalid")
    (tmp_path / "stranger" / "README.md").write_text("not a workspace\n")
    stranger.git.add("--", "README.md")
    stranger.git.commit("-m", "unrelated")
    stranger.create_remote("origin", str(hub))
    stranger.git.push("origin", "main:main")

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "a: b\n"}))


# -- Previewing ---------------------------------------------------------------


def test_a_preview_reports_what_each_side_alone_holds(
    tmp_path: Path, hub: Path
) -> None:
    enrollment.enroll(
        str(hub),
        _workspace(
            tmp_path / "mac", **{CONFIG: "name: hub\n", "config/roles.yml": "r\n"}
        ),
    )
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", ROLES: "a: b\n"}
    )

    preview = enrollment.preview_enrollment(str(hub), joining)

    assert "config/roles.yml" in preview.hub_only
    assert ROLES in preview.device_only
    assert CONFIG in preview.differing


def test_a_preview_leaves_the_workspace_unconnected(tmp_path: Path, hub: Path) -> None:
    """A preview the user does not act on must not start synchronizing."""
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    enrollment.preview_enrollment(str(hub), joining)

    assert not LocalSyncRepository(joining).has_remote()
    assert (joining / ".guildbotics" / CONFIG).read_text() == "name: mine\n"


def test_an_empty_hub_has_nothing_to_preview(tmp_path: Path, hub: Path) -> None:
    """Registering gives the hub its first content, so there is no other side
    to compare against -- and asking anyway would make this workspace a
    repository for an answer that is known in advance."""
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.preview_enrollment(str(hub), root)


def test_a_preview_reports_what_cannot_be_shared_yet(tmp_path: Path, hub: Path) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(
        tmp_path / "windows",
        **{CONFIG: "name: mine\n", "state/chat_state/broken.json": "{not json"},
    )

    preview = enrollment.preview_enrollment(str(hub), joining)

    assert [change.path for change in preview.unsendable] == [
        "state/chat_state/broken.json"
    ]


def test_a_preview_and_the_join_that_follows_agree(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", ROLES: "a: b\n"}
    )

    preview = enrollment.preview_enrollment(str(hub), joining)
    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert result.adopted == tuple(sorted([*preview.hub_only, *preview.differing]))


# -- Reconnecting to a rebuilt hub --------------------------------------------


def test_a_device_reconnects_to_a_rebuilt_hub_without_losing_its_own_work(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    """Rebuilding a hub is the join flow again, from whichever device is chosen."""
    origin = _workspace(tmp_path / "mac", **{CONFIG: "name: shared\n"})
    enrollment.enroll(str(hub), origin)
    other = tmp_path / "windows"
    enrollment.clone_workspace(str(hub), other)
    _write(other, ROLES, "a: b\n")

    new_hub = tmp_path / "new-hub.git"
    Repo.init(new_hub, bare=True, initial_branch="main")
    enrollment.enroll(str(new_hub), origin)
    enrollment.enroll(str(new_hub), other, record_rejection=recorder)

    assert _hub_file(new_hub, CONFIG) == "name: shared\n"
    assert _hub_file(new_hub, ROLES) == "a: b\n"


def test_a_hub_rebuilt_at_the_same_address_receives_the_content_again(
    tmp_path: Path, hub: Path
) -> None:
    """The realistic rebuild: the replacement hub lives where the old one did.

    Nothing about the remote changes, so the only thing that can tell this
    device the hub is empty is the fetch itself. Left unpruned, the
    remote-tracking ref from the hub that is gone keeps describing content the
    replacement has never seen, and this device sends it nothing while
    reporting itself in sync.
    """
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: shared\n"})
    registered = enrollment.enroll(str(hub), root)
    device: Device = make_device(
        root, hub, device_id="device-mac", workspace_id=registered.workspace_id
    )
    # Fetching at least once is what leaves a remote-tracking ref behind.
    device.manager.synchronize()

    shutil.rmtree(hub)
    Repo.init(hub, bare=True, initial_branch="main")
    enrollment.enroll(str(hub), root)

    assert _hub_file(hub, CONFIG) == "name: shared\n"


def test_the_device_that_reconnects_keeps_synchronizing_afterwards(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})
    registered = enrollment.enroll(str(hub), root)
    device: Device = make_device(
        root, hub, device_id="device-mac", workspace_id=registered.workspace_id
    )

    device.write(ROLES, "a: b\n")
    device.manager.synchronize()

    assert _hub_file(hub, ROLES) == "a: b\n"


# -- What a join must not destroy ---------------------------------------------


def test_a_file_that_cannot_be_shared_yet_survives_the_join(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    """It is the user's unfinished work, not a losing side of a race: the hub
    has no version of it that supersedes anything, and it is not committed, so
    replacing it would lose the edit outright."""
    broken = "state/chat_state/pending.json"
    enrollment.enroll(
        str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n", broken: "{}"})
    )
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", broken: "{not json"}
    )

    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert (joining / ".guildbotics" / broken).read_text() == "{not json"
    assert broken not in result.adopted
    assert [change.path for change in result.unsendable] == [broken]


# -- Reconnecting to a hub this workspace shares history with -----------------


def _rebuilt_hub(tmp_path: Path, origin: Path) -> Path:
    """A new hub carrying the same history, as a planned hub move produces."""
    new_hub = tmp_path / "new-hub.git"
    Repo.init(new_hub, bare=True, initial_branch="main")
    enrollment.enroll(str(new_hub), origin)
    return new_hub


def test_a_reconnecting_device_keeps_changes_the_hub_never_saw(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    """Both sides holding a file is not a conflict when only one of them
    changed it since the commit they last agreed on."""
    origin = _workspace(tmp_path / "mac", **{CONFIG: "name: shared\n"})
    with _as_machine(tmp_path, "mac"):
        enrollment.enroll(str(hub), origin)
    other = tmp_path / "windows"
    with _as_machine(tmp_path, "windows"):
        enrollment.clone_workspace(str(hub), other)
        (other / ".guildbotics" / CONFIG).write_text("name: edited\n", encoding="utf-8")
    with _as_machine(tmp_path, "mac"):
        rebuilt = _rebuilt_hub(tmp_path, origin)

    with _as_machine(tmp_path, "windows"):
        result = enrollment.enroll(str(rebuilt), other, record_rejection=recorder)

    assert result.mode == "reconnect"
    assert result.rejection_id is None
    assert (other / ".guildbotics" / CONFIG).read_text() == "name: edited\n"


def test_a_reconnecting_device_still_yields_where_both_sides_changed(
    tmp_path: Path,
    hub: Path,
    recorder: enrollment.RejectionRecorder,
    rejections: list[dict[str, Any]],
) -> None:
    origin = _workspace(tmp_path / "mac", **{CONFIG: "name: shared\n"})
    with _as_machine(tmp_path, "mac"):
        enrollment.enroll(str(hub), origin)
    other = tmp_path / "windows"
    with _as_machine(tmp_path, "windows"):
        enrollment.clone_workspace(str(hub), other)
        (other / ".guildbotics" / CONFIG).write_text("name: mine\n", encoding="utf-8")
    (origin / ".guildbotics" / CONFIG).write_text("name: theirs\n", encoding="utf-8")
    with _as_machine(tmp_path, "mac"):
        rebuilt = _rebuilt_hub(tmp_path, origin)

    with _as_machine(tmp_path, "windows"):
        result = enrollment.enroll(str(rebuilt), other, record_rejection=recorder)

    assert result.mode == "reconnect"
    assert result.rejection_id is not None
    assert (other / ".guildbotics" / CONFIG).read_text() == "name: theirs\n"
    assert CONFIG in rejections[0]["paths"]


def test_a_first_meeting_is_still_a_join(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    assert (
        enrollment.enroll(str(hub), joining, record_rejection=recorder).mode == "join"
    )


# -- A hub that was refused leaves nothing behind -----------------------------


def test_a_refused_hub_leaves_the_workspace_unconnected(
    tmp_path: Path, hub: Path
) -> None:
    """Otherwise the next start finds a connected workspace and runs a queue
    against a hub the user was told they could not use."""
    stranger = Repo.init(tmp_path / "stranger", initial_branch="main")
    stranger.git.config("user.name", "someone")
    stranger.git.config("user.email", "someone@example.invalid")
    (tmp_path / "stranger" / "README.md").write_text("not a workspace\n")
    stranger.git.add("--", "README.md")
    stranger.git.commit("-m", "unrelated")
    stranger.create_remote("origin", str(hub))
    stranger.git.push("origin", "main:main")
    root = _workspace(tmp_path / "mac", **{CONFIG: "a: b\n"})

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.enroll(str(hub), root)

    assert not LocalSyncRepository(root).has_remote()


def test_an_unreachable_hub_is_reported_rather_than_raised_as_a_git_error(
    tmp_path: Path,
) -> None:
    """ "The hub is off" and "your key is not registered" arrive here, so they
    have to be something the layer above can put in front of the user."""
    root = _workspace(tmp_path / "mac", **{CONFIG: "a: b\n"})

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.enroll(str(tmp_path / "no-such-hub.git"), root)

    assert not LocalSyncRepository(root).has_remote()


def test_a_preview_tells_an_unreachable_hub_from_an_empty_one(tmp_path: Path) -> None:
    """Both look like "nothing to fetch"; only one of them means the user is
    about to register with an empty hub."""
    root = _workspace(tmp_path / "mac", **{CONFIG: "a: b\n"})

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.preview_enrollment(str(tmp_path / "no-such-hub.git"), root)


def test_a_preview_keeps_no_content_from_a_hub_that_was_not_joined(
    tmp_path: Path, hub: Path
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    enrollment.preview_enrollment(str(hub), joining)

    refs = Repo(joining / ".guildbotics").git.for_each_ref("--format=%(refname)")
    assert "hub-preview" not in refs


def test_a_repeated_join_records_one_rejection(
    tmp_path: Path,
    hub: Path,
    recorder: enrollment.RejectionRecorder,
    rejections: list[dict[str, Any]],
) -> None:
    """A retry after a lost push must not tell the user twice about one thing."""
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})
    first = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    again = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert len(rejections) == 1
    assert again.rejection_id is None or again.rejection_id == first.rejection_id


def test_a_save_made_while_the_hub_is_reached_survives_as_a_rejection(
    tmp_path: Path,
    hub: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: enrollment.RejectionRecorder,
) -> None:
    """Reaching the hub is the one interval a writer cannot protect itself in.

    The lock cannot be held across the network, so a save made there holds it
    correctly and is still only in the working tree -- where adopting the hub's
    version of the same file overwrites it. Uncommitted, it would not be in the
    rejected ref either, so the user's edit would be gone with nothing saying
    so. Committing again inside the join's own lock is what gives it a name.
    """
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})
    original_fetch = LocalSyncRepository.fetch
    saved: list[str] = []

    def fetch_then_save(self: LocalSyncRepository) -> None:
        original_fetch(self)
        if not saved:
            saved.append("done")
            _write(joining, CONFIG, "name: saved while connecting\n")

    monkeypatch.setattr(LocalSyncRepository, "fetch", fetch_then_save)

    result = enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert (joining / ".guildbotics" / CONFIG).read_text() == "name: hub\n"
    assert result.rejection_id is not None
    rejected = Repo(joining / ".guildbotics").git.show(
        f"{REJECTED_REF_PREFIX}/{result.rejection_id}:{CONFIG}"
    )
    assert rejected == "name: saved while connecting"


def test_a_failed_hub_change_keeps_the_hub_that_was_working(
    tmp_path: Path, hub: Path
) -> None:
    """Leaving the refused URL would point the queue at a hub the user was told
    they could not use, and clearing it would disconnect a working one."""
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})
    enrollment.enroll(str(hub), root)

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.enroll(str(tmp_path / "no-such-hub.git"), root)

    assert LocalSyncRepository(root).remote_url() == str(hub)


def test_a_workspace_still_synchronizes_after_a_failed_hub_change(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})
    registered = enrollment.enroll(str(hub), root)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.enroll(str(tmp_path / "no-such-hub.git"), root)
    device = make_device(
        root, hub, device_id="device-mac", workspace_id=registered.workspace_id
    )

    device.write(ROLES, "a: b\n")
    device.manager.synchronize()

    assert _hub_file(hub, ROLES) == "a: b\n"
