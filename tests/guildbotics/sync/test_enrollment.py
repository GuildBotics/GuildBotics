"""Connecting a workspace to a hub: registering, joining, and taking a copy."""

from __future__ import annotations

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
HOTKEYS = "config/hotkeys.yml"


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

    assert result.mode == "registered"
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

    assert result.mode == "joined"
    assert (joining / ".guildbotics" / CONFIG).read_text() == "name: hub\n"
    assert CONFIG in result.adopted


def test_joining_keeps_and_shares_a_file_only_this_machine_has(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", HOTKEYS: "a: b\n"}
    )

    enrollment.enroll(str(hub), joining, record_rejection=recorder)

    assert (joining / ".guildbotics" / HOTKEYS).read_text() == "a: b\n"
    assert _hub_file(hub, HOTKEYS) == "a: b\n"


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
        tmp_path / "windows", **{CONFIG: "name: mine\n", HOTKEYS: "a: b\n"}
    )

    preview = enrollment.preview_enrollment(str(hub), joining)

    assert "config/roles.yml" in preview.hub_only
    assert HOTKEYS in preview.device_only
    assert CONFIG in preview.differing


def test_a_preview_leaves_the_workspace_unconnected(tmp_path: Path, hub: Path) -> None:
    """A preview the user does not act on must not start synchronizing."""
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(tmp_path / "windows", **{CONFIG: "name: mine\n"})

    enrollment.preview_enrollment(str(hub), joining)

    assert not LocalSyncRepository(joining).has_remote()
    assert (joining / ".guildbotics" / CONFIG).read_text() == "name: mine\n"


def test_a_preview_of_an_empty_hub_reports_a_registration(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})

    preview = enrollment.preview_enrollment(str(hub), root)

    assert preview.hub_workspace_id is None
    assert preview.differing == ()


def test_a_preview_reports_what_cannot_be_shared_yet(tmp_path: Path, hub: Path) -> None:
    root = _workspace(
        tmp_path / "mac",
        **{CONFIG: "name: demo\n", "state/chat_state/broken.json": "{not json"},
    )

    preview = enrollment.preview_enrollment(str(hub), root)

    assert [change.path for change in preview.unsendable] == [
        "state/chat_state/broken.json"
    ]


def test_a_preview_and_the_join_that_follows_agree(
    tmp_path: Path, hub: Path, recorder: enrollment.RejectionRecorder
) -> None:
    enrollment.enroll(str(hub), _workspace(tmp_path / "mac", **{CONFIG: "name: hub\n"}))
    joining = _workspace(
        tmp_path / "windows", **{CONFIG: "name: mine\n", HOTKEYS: "a: b\n"}
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
    (other / ".guildbotics" / HOTKEYS).write_text("a: b\n", encoding="utf-8")

    new_hub = tmp_path / "new-hub.git"
    Repo.init(new_hub, bare=True, initial_branch="main")
    enrollment.enroll(str(new_hub), origin)
    enrollment.enroll(str(new_hub), other, record_rejection=recorder)

    assert _hub_file(new_hub, CONFIG) == "name: shared\n"
    assert _hub_file(new_hub, HOTKEYS) == "a: b\n"


def test_the_device_that_reconnects_keeps_synchronizing_afterwards(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac", **{CONFIG: "name: demo\n"})
    registered = enrollment.enroll(str(hub), root)
    device: Device = make_device(
        root, hub, device_id="device-mac", workspace_id=registered.workspace_id
    )

    device.write(HOTKEYS, "a: b\n")
    device.manager.synchronize()

    assert _hub_file(hub, HOTKEYS) == "a: b\n"
