"""How shared state travels, converges, and stops, between two real devices."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from git import GitCommandError, Repo

from guildbotics.sync.local_repository import REJECTED_REF_PREFIX, LocalSyncRepository
import guildbotics.sync.manager as manager_module
from guildbotics.sync.manager import SharedDataAnomaly
from guildbotics.utils.workspace_sync_port import (
    ChangeSet,
    dump_shared_json,
    set_workspace_sync_port,
    write_shared_text,
)
from guildbotics.workspace.identity import WorkspaceIdentity
from tests.guildbotics.sync.conftest import WORKSPACE_ID, Device, make_device
from guildbotics.utils.shared_write_lock import shared_write_lock
from tests.guildbotics.workspace.test_config_repository import shared_write_lock_is_held

CONFIG = "config/team/project.yml"
#: A shared record whose syntax the boundary still refuses to send.
BROKEN = "state/chat_state/slack/aiko/channels/C1.json"


def _activity_event(event_id: str, summary: str) -> str:
    return dump_shared_json(
        {
            "schema_version": 1,
            "event_id": event_id,
            "occurred_at": "2026-08-10T00:00:00Z",
            "kind": "github.push",
            "safe_summary": summary,
        }
    )


def _identity(workspace_id: str) -> str:
    return dump_shared_json(
        WorkspaceIdentity(
            workspace_id=workspace_id, created_at="2026-08-01T00:00:00Z"
        ).model_dump()
    )


def _hub_bytes(hub: Path, path: str) -> bytes | None:
    try:
        return bytes(
            Repo(hub).git.cat_file(
                "blob",
                f"main:{path}",
                stdout_as_string=False,
                strip_newline_in_stdout=False,
            )
        )
    except GitCommandError:
        return None


def _hub_file(hub: Path, path: str) -> str | None:
    data = _hub_bytes(hub, path)
    return None if data is None else data.decode("utf-8")


# -- Sending and receiving ----------------------------------------------------


def test_saved_state_reaches_the_hub(first: Device, hub: Path) -> None:
    first.write(CONFIG, "language: ja\n")

    status = first.manager.synchronize()

    assert _hub_file(hub, CONFIG) == "language: ja\n"
    assert status.state == "idle"
    assert status.ahead_count == 0
    assert status.last_success_at is not None


def test_another_device_receives_what_the_hub_holds(
    first: Device, second: Device
) -> None:
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()

    second.manager.synchronize()

    assert second.read(CONFIG) == "language: ja\n"


def test_a_deletion_travels_like_any_other_change(
    first: Device, second: Device, hub: Path
) -> None:
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()
    second.manager.synchronize()

    first.delete(CONFIG)
    first.manager.synchronize()
    second.manager.synchronize()

    assert _hub_file(hub, CONFIG) is None
    assert not second.exists(CONFIG)


def test_a_member_avatar_is_shared_as_a_binary(first: Device, hub: Path) -> None:
    """Avatars are the only binary normal synchronization carries, so the
    commit boundary must accept them rather than treat them as damage."""
    avatar = "config/team/members/aiko/avatar.png"
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\n\n"
    first.write_bytes(avatar, data)

    first.manager.synchronize()

    assert _hub_bytes(hub, avatar) == data


def test_an_edit_made_outside_guildbotics_is_recovered_by_the_rescan(
    first: Device, hub: Path
) -> None:
    """An external editor sends no save notification; the working tree scan is
    what picks the change up, so no file watcher is needed."""
    path = first.shared / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("language: en\n", encoding="utf-8")

    first.manager.synchronize()

    assert _hub_file(hub, CONFIG) == "language: en\n"


# -- Convergence --------------------------------------------------------------


def test_changes_to_different_files_are_both_kept(
    first: Device, second: Device, hub: Path
) -> None:
    first.write(CONFIG, "language: ja\n")
    second.write("state/events/2026/08/e1.json", _activity_event("e1", "pushed"))
    first.manager.synchronize()

    second.manager.synchronize()

    assert second.read(CONFIG) == "language: ja\n"
    assert _hub_file(hub, "state/events/2026/08/e1.json") is not None
    assert second.rejections == []


def test_the_change_that_reached_the_hub_first_wins(
    first: Device, second: Device, hub: Path
) -> None:
    first.write(CONFIG, "language: ja\n")
    second.write(CONFIG, "language: en\n")
    first.manager.synchronize()

    status = second.manager.synchronize()

    assert second.read(CONFIG) == "language: ja\n"
    assert _hub_file(hub, CONFIG) == "language: ja\n"
    assert status.state == "idle"
    assert [rejection["paths"] for rejection in second.rejections] == [[CONFIG]]


def test_the_losing_change_stays_readable_on_the_device_that_made_it(
    first: Device, second: Device
) -> None:
    """§7.4: recovery is a manual, source-device-only procedure, so the content
    must be reachable from the ``rejection_id`` the activity event carries."""
    first.write(CONFIG, "language: ja\n")
    second.write(CONFIG, "language: en\n")
    first.manager.synchronize()
    second.manager.synchronize()

    rejection_id = second.rejections[0]["rejection_id"]
    ref = f"{REJECTED_REF_PREFIX}/{rejection_id}"
    assert second.repository.read_blob(ref, CONFIG) == b"language: en\n"
    assert first.repository.read_blob(ref, CONFIG) is None


def test_only_the_overlapping_part_of_a_losing_commit_is_dropped(
    first: Device, second: Device, hub: Path
) -> None:
    first.write(CONFIG, "language: ja\n")
    second.write(CONFIG, "language: en\n")
    second.write("state/events/2026/08/e2.json", _activity_event("e2", "kept"))
    first.manager.synchronize()

    second.manager.synchronize()

    assert second.read(CONFIG) == "language: ja\n"
    assert _hub_file(hub, "state/events/2026/08/e2.json") is not None


def test_stopping_after_stashing_does_not_stash_the_same_commit_twice(
    first: Device, second: Device
) -> None:
    """Failure injection: the process dies between saving the rejected ref and
    adopting the hub's content. The restart must converge, not stash again."""
    first.write(CONFIG, "language: ja\n")
    second.write(CONFIG, "language: en\n")
    first.manager.synchronize()

    def die(_commit: str) -> None:
        raise OSError("stopped before adopting the hub content")

    original = second.repository.move_to
    second.repository.move_to = die  # type: ignore[method-assign]
    second.manager.synchronize()
    second.repository.move_to = original  # type: ignore[method-assign]

    second.manager.synchronize()

    assert second.read(CONFIG) == "language: ja\n"
    assert len(second.rejections) == 1
    refs = Repo(second.shared).git.for_each_ref(REJECTED_REF_PREFIX)
    assert len(refs.splitlines()) == 1


def test_a_lost_push_response_is_settled_by_the_hub_head(
    first: Device, hub: Path
) -> None:
    """Failure injection: the push lands but the answer never arrives. The hub's
    own head decides, so no second commit is created."""
    first.write(CONFIG, "language: ja\n")
    real_push = first.repository.push

    def push_then_lose_the_answer() -> None:
        real_push()
        raise GitCommandError("push", 128, b"connection reset")

    first.repository.push = push_then_lose_the_answer  # type: ignore[method-assign]
    status = first.manager.synchronize()

    assert status.state == "idle"
    assert status.last_error_code is None
    assert _hub_file(hub, CONFIG) == "language: ja\n"
    assert len(list(Repo(hub).iter_commits("main"))) == 2


def test_a_hub_that_moved_after_the_decision_is_redone_and_settles(
    first: Device, second: Device, hub: Path
) -> None:
    """§15.5 rule 6: losing the race between deciding and pushing gives way and
    redoes the same decision, rather than forcing the local commit through."""
    first.write(CONFIG, "language: ja\n")
    second.write("state/events/2026/08/e4.json", _activity_event("e4", "windows"))
    real_push = second.repository.push
    pushes = 0

    def lose_the_race_once() -> None:
        nonlocal pushes
        pushes += 1
        if pushes == 1:
            first.manager.synchronize()
        real_push()

    second.repository.push = lose_the_race_once  # type: ignore[method-assign]
    status = second.manager.synchronize()

    assert pushes == 2
    assert status.state == "idle"
    assert second.read(CONFIG) == "language: ja\n"
    assert _hub_file(hub, "state/events/2026/08/e4.json") is not None


def test_a_hub_that_keeps_moving_is_redone_a_bounded_number_of_times(
    first: Device,
) -> None:
    attempts = 0

    def always_behind() -> None:
        nonlocal attempts
        attempts += 1
        raise GitCommandError(
            "push", 1, b"! [rejected] main -> main (non-fast-forward)"
        )

    first.write(CONFIG, "language: ja\n")
    first.repository.push = always_behind  # type: ignore[method-assign]

    status = first.manager.synchronize()

    assert attempts == 5
    assert status.last_error_code == "push_retry_exhausted"
    assert status.ahead_count == 1


# -- Changes that cannot be sent ---------------------------------------------


def test_an_invalid_file_is_held_back_while_the_rest_is_sent(
    first: Device, hub: Path
) -> None:
    first.write(BROKEN, "{not json}")
    first.write(CONFIG, "language: ja\n")

    status = first.manager.synchronize()

    assert [held.path for held in status.invalid_paths] == [BROKEN]
    assert "not valid JSON" in status.invalid_paths[0].reason
    assert _hub_file(hub, BROKEN) is None
    assert _hub_file(hub, CONFIG) == "language: ja\n"


def test_a_held_back_change_is_not_overwritten_by_the_hub(
    first: Device, second: Device, hub: Path
) -> None:
    """A file that fails validation was never shareable, so it never loses a
    race -- but it is still the user's work and must survive convergence."""
    first.write(BROKEN, '{"cursor": "from mac"}')
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()
    second.write(BROKEN, "{not json}")

    status = second.manager.synchronize()

    assert [held.path for held in status.invalid_paths] == [BROKEN]
    assert second.read(BROKEN) == "{not json}"
    assert second.read(CONFIG) == "language: ja\n"
    assert "from mac" in (_hub_file(hub, BROKEN) or "")


def test_a_repaired_file_is_sent_by_the_next_scan(first: Device, hub: Path) -> None:
    first.write(BROKEN, "{not json}")
    first.manager.synchronize()

    first.write(BROKEN, '{"cursor": "repaired"}')
    status = first.manager.synchronize()

    assert status.invalid_paths == ()
    assert _hub_file(hub, BROKEN) is not None


# -- Damaged shared data ------------------------------------------------------


def test_two_devices_minting_one_event_id_stops_the_queue(
    first: Device, second: Device, hub: Path
) -> None:
    """An immutable record created twice is damage, not a concurrent edit, so
    it is never settled by overwriting one of them."""
    path = "state/events/2026/08/collision.json"
    first.write(path, _activity_event("collision", "from mac"))
    second.write(path, _activity_event("collision", "from windows"))
    first.manager.synchronize()

    status = second.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "immutable_id_collision"
    assert second.rejections == []
    assert "from mac" in (_hub_file(hub, path) or "")


def test_another_workspace_on_the_hub_stops_the_queue(
    tmp_path: Path, hub: Path, first: Device
) -> None:
    stranger = make_device(
        tmp_path / "stranger",
        hub,
        device_id="device-stranger",
        workspace_id="0198ab00-0000-7000-8000-00000000ffff",
    )

    status = stranger.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "workspace_identity_mismatch"


def test_a_record_from_a_newer_build_asks_for_an_update(
    first: Device, second: Device
) -> None:
    first.write(
        "state/workspace.json",
        dump_shared_json(
            {
                "schema_version": 2,
                "workspace_id": WORKSPACE_ID,
                "created_at": "2026-08-01T00:00:00Z",
            }
        ),
    )
    # The sending device cannot validate it either, so publish it directly.
    Repo(first.shared).git.add("--", "state/workspace.json")
    Repo(first.shared).git.commit("-m", "future schema")
    first.repository.push()

    status = second.manager.synchronize()

    assert status.state == "update_required"
    assert status.last_error_code == "schema_version_ahead"


def test_a_stopped_queue_refuses_new_work_until_it_is_resumed(
    first: Device, second: Device
) -> None:
    first.write(CONFIG, "language: ja\n")
    second.write("state/events/2026/08/c.json", _activity_event("c", "windows"))
    first.write("state/events/2026/08/c.json", _activity_event("c", "mac"))
    first.manager.synchronize()
    second.manager.synchronize()

    assert not second.manager.shared_state_changed(
        ChangeSet(change_id="later", operation="update", paths=("config/x.yml",))
    )
    assert second.manager.synchronize().state == "invalid_shared_state"

    (second.shared / "state/events/2026/08/c.json").unlink()
    assert second.manager.resume().state == "idle"


def test_unrelated_histories_are_damage_not_a_concurrent_update(
    tmp_path: Path, first: Device
) -> None:
    """A hub built from scratch rather than from a copy carries the same
    workspace but shares no history, which no convergence rule can settle."""
    other_hub = tmp_path / "other.git"
    Repo.init(other_hub, bare=True, initial_branch="main")
    stranger = make_device(tmp_path / "stranger", other_hub, device_id="device-x")
    stranger.write("state/workspace.json", _identity(WORKSPACE_ID))
    stranger.write(CONFIG, "language: en\n")
    stranger.manager.synchronize()

    first.repository.set_remote(str(other_hub))
    status = first.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "unrelated_histories"


def test_a_hub_with_commits_but_no_workspace_identity_stops_the_queue(
    tmp_path: Path, first: Device
) -> None:
    """The identity is the only thing that keeps two workspaces apart, so a hub
    that has content but cannot name its workspace is not joined."""
    other_hub = tmp_path / "nameless.git"
    Repo.init(other_hub, bare=True, initial_branch="main")
    stranger = make_device(tmp_path / "nameless", other_hub, device_id="device-x")
    stranger.write(CONFIG, "language: en\n")
    stranger.manager.synchronize()

    first.repository.set_remote(str(other_hub))
    status = first.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "missing_workspace_identity"


def test_deleting_the_workspace_identity_is_never_shared(
    first: Device, hub: Path
) -> None:
    """Propagating the deletion would leave no device able to tell workspaces
    apart again, so it stops here instead of becoming a change to send."""
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()
    (first.shared / "state" / "workspace.json").unlink()

    status = first.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "missing_workspace_identity"
    assert _hub_file(hub, "state/workspace.json") is not None


def test_an_unreadable_local_identity_stops_the_queue(first: Device) -> None:
    """The parse must not escape: an exception leaving ``synchronize`` would end
    the background worker, leaving a device that looks idle while it has
    stopped synchronizing."""
    (first.shared / "state" / "workspace.json").write_text("{ broken", encoding="utf-8")

    status = first.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "invalid_shared_file"


def test_a_local_identity_from_a_newer_build_asks_for_an_update(
    first: Device,
) -> None:
    first.write(
        "state/workspace.json",
        dump_shared_json(
            {
                "schema_version": 2,
                "workspace_id": WORKSPACE_ID,
                "created_at": "2026-08-01T00:00:00Z",
            }
        ),
    )

    status = first.manager.synchronize()

    assert status.state == "update_required"
    assert status.last_error_code == "schema_version_ahead"


def test_the_queue_survives_an_unexpected_error(first: Device) -> None:
    """A defect must not silently end the worker. The loop keeps running and
    the status says something went wrong."""

    def fail() -> None:
        raise RuntimeError("a defect nobody predicted")

    first.repository.verify_boundary = fail  # type: ignore[method-assign]
    first.manager.start()
    try:
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline
            and first.manager.status().last_error_code != "unexpected_error"
        ):
            time.sleep(0.05)
    finally:
        first.manager.stop(timeout=10)

    assert first.manager.status().last_error_code == "unexpected_error"


def test_a_copy_that_became_another_workspace_stops_the_queue(
    first: Device,
) -> None:
    first.write(
        "state/workspace.json", _identity("0198ab00-0000-7000-8000-0000000000ff")
    )

    status = first.manager.synchronize()

    assert status.state == "invalid_shared_state"
    assert status.last_error_code == "workspace_identity_mismatch"


# -- The execution barrier ----------------------------------------------------


def test_a_barrier_completes_once_the_change_reaches_the_hub(
    first: Device,
) -> None:
    change = first.write(CONFIG, "language: ja\n")

    first.manager.synchronize()

    assert first.manager.await_pushed(change.change_id) is True


def test_a_barrier_does_not_complete_while_the_hub_is_unreachable(
    tmp_path: Path, hub: Path
) -> None:
    offline = make_device(
        tmp_path / "offline", tmp_path / "missing.git", device_id="device-offline"
    )
    change = offline.write(CONFIG, "language: ja\n")

    status = offline.manager.synchronize()

    assert status.state == "unreachable"
    assert offline.manager.await_pushed(change.change_id) is False


def test_a_barrier_does_not_complete_for_a_change_the_hub_refused(
    first: Device, second: Device
) -> None:
    first.write(CONFIG, "language: ja\n")
    change = second.write(CONFIG, "language: en\n")
    first.manager.synchronize()

    second.manager.synchronize()

    assert second.manager.await_pushed(change.change_id) is False


def test_a_barrier_does_not_complete_while_shared_data_is_damaged(
    first: Device, second: Device
) -> None:
    path = "state/events/2026/08/collision.json"
    first.write(path, _activity_event("collision", "from mac"))
    change = second.write(path, _activity_event("collision", "from windows"))
    first.manager.synchronize()

    second.manager.synchronize()

    assert second.manager.await_pushed(change.change_id) is False


def test_a_barrier_does_not_complete_for_a_change_held_back_by_validation(
    first: Device,
) -> None:
    change = first.write(BROKEN, "{not json}")

    first.manager.synchronize()

    assert first.manager.await_pushed(change.change_id) is False


def test_an_unknown_change_is_never_treated_as_confirmed(first: Device) -> None:
    assert first.manager.await_pushed("never-announced") is False


# -- The queue ----------------------------------------------------------------


def test_the_queue_checks_the_hub_on_its_own_without_a_notification(
    tmp_path: Path, hub: Path, first: Device
) -> None:
    """§7.2: with hub notifications gone, a device still converges on its own
    timer, without any user action."""
    listener = make_device(
        tmp_path / "listener",
        hub,
        device_id="device-listener",
        fallback_interval=0.05,
    )
    listener.manager.synchronize()
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()

    listener.manager.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not listener.exists(CONFIG):
            time.sleep(0.05)
    finally:
        listener.manager.stop()

    assert listener.read(CONFIG) == "language: ja\n"


def test_a_worker_that_outlives_its_stop_blocks_a_second_one(
    first: Device,
) -> None:
    """One device runs one queue. A fetch can block for longer than the stop
    timeout, and starting a second worker beside it would put two threads on
    one repository."""
    release = threading.Event()
    first.repository.fetch = lambda: release.wait(10)  # type: ignore[method-assign]
    first.write(CONFIG, "language: ja\n")
    first.manager.start()
    try:
        assert first.manager.stop(timeout=0.2) is False
        assert first.manager.start() is False
    finally:
        release.set()
        assert first.manager.stop(timeout=10) is True

    assert first.manager.start() is True
    assert first.manager.stop(timeout=10) is True


def test_a_worker_stopped_before_a_restart_never_wakes_up_again(
    first: Device,
) -> None:
    """The restarted queue must not revive the previous worker, which would be
    the second thread the stop was meant to remove."""
    first.manager.start()
    assert first.manager.stop(timeout=10) is True
    stopped = first.manager._stopping  # noqa: SLF001

    assert first.manager.start() is True
    try:
        assert stopped.is_set()
        assert first.manager._stopping is not stopped  # noqa: SLF001
    finally:
        first.manager.stop(timeout=10)


def test_a_settled_change_is_remembered_while_the_queue_is_far_below_its_cap(
    first: Device,
) -> None:
    """Forgetting is for the cap alone; a barrier asking about its own change
    must not be told it is unknown just because others settled after it."""
    changes = [
        first.write(
            f"state/events/2026/08/keep-{index}.json", _activity_event(f"k{index}", "x")
        )
        for index in range(50)
    ]

    first.manager.synchronize()

    assert all(first.manager.await_pushed(change.change_id) for change in changes)


def test_a_burst_of_saves_becomes_one_commit(tmp_path: Path, hub: Path) -> None:
    device = make_device(
        tmp_path / "busy", hub, device_id="device-busy", fallback_interval=30.0
    )
    device.write(
        "state/workspace.json",
        dump_shared_json(
            WorkspaceIdentity(
                workspace_id=WORKSPACE_ID, created_at="2026-08-01T00:00:00Z"
            ).model_dump()
        ),
    )
    device.manager.synchronize()
    before = len(list(Repo(hub).iter_commits("main")))

    for index in range(5):
        device.write(
            f"state/events/2026/08/burst-{index}.json",
            _activity_event(f"burst-{index}", "burst"),
        )
    device.manager.synchronize()

    assert len(list(Repo(hub).iter_commits("main"))) == before + 1


def test_committing_the_working_tree_holds_the_shared_write_lock(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit reads the tree as one state, so a config save must be excluded.

    Without it, a save writing several files has part of them committed here
    and the rest in the next cycle.
    """
    held = _lock_state_during(monkeypatch, "commit", first.root)
    first.write(CONFIG, "language: ja\n")

    first.manager.synchronize()

    assert held == [True]


def test_adopting_the_hub_content_holds_the_shared_write_lock(
    first: Device, second: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that landed here would be overwritten by content it never saw --
    and then committed and pushed as if the user had made that change."""
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()
    held = _lock_state_during(monkeypatch, "restore_from_index", second.root)

    second.manager.synchronize()

    assert held == [True]


def test_converging_never_lets_go_between_the_commit_and_the_adoption(
    first: Device, second: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of it is one span, not two with a gap in the middle.

    Nothing between the commit and the checkout waits on the hub -- the fetch
    already happened -- so a writer let in there gains nothing and loses its
    work: whatever it saved is still only in the working tree when the checkout
    takes it away, and being uncommitted it is not rejected on the record
    either. Deciding what to adopt is what runs in that gap, so the lock is
    observed there.
    """
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()
    second.write("state/events/2026/08/local.json", _activity_event("local", "s"))
    held = _lock_state_during(monkeypatch, "changed_paths", second.root)

    second.manager.synchronize()

    assert held and all(held)


def _lock_state_during(
    monkeypatch: pytest.MonkeyPatch, method: str, workspace_root: Path
) -> list[bool]:
    """Record whether the shared-write lock was held each time ``method`` ran."""
    observed: list[bool] = []
    original = getattr(LocalSyncRepository, method)

    def spy(self: LocalSyncRepository, *args: Any, **kwargs: Any) -> Any:
        observed.append(shared_write_lock_is_held(workspace_root))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LocalSyncRepository, method, spy)
    return observed


def test_a_busy_workspace_is_not_reported_as_an_unreachable_hub(
    first: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save holding the workspace's files says nothing about the hub.

    A lock timeout belongs to the ``OSError`` family that this queue catches to
    mean the environment failed, so without naming it the sidebar would tell
    the user their hub is unreachable while a local save was simply in the way.
    """

    @contextmanager
    def brief(workspace_root: Path | None = None, **_: Any) -> Iterator[Any]:
        with shared_write_lock(workspace_root, timeout=0.05) as handle:
            yield handle

    monkeypatch.setattr(manager_module, "shared_write_lock", brief)
    first.write(CONFIG, "language: ja\n")
    # The lock re-enters within one thread, so the save in the way has to be a
    # genuinely different thread holding it while the queue runs.
    locked = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with shared_write_lock(first.root):
            locked.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert locked.wait(5)
        status = first.manager.synchronize()
    finally:
        release.set()
        holder.join(10)

    assert not holder.is_alive()
    assert status.last_error_code == "local_write_busy"
    assert status.state != "unreachable"


def test_synchronize_is_serialized_between_threads(first: Device) -> None:
    first.write(CONFIG, "language: ja\n")
    results: list[str] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(first.manager.synchronize().state)
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert results == ["idle"] * 4


def test_a_workspace_with_no_hub_keeps_its_commits(tmp_path: Path) -> None:
    """§7.5: a device that has not been connected still commits locally, and
    says why nothing is shared."""
    (tmp_path / "solo" / ".guildbotics" / "state").mkdir(parents=True)
    solo = make_device(tmp_path / "solo", tmp_path / "unused.git", device_id="solo")
    Repo(solo.shared).delete_remote("origin")
    solo.write(CONFIG, "language: ja\n")

    status = solo.manager.synchronize()

    assert status.state == "unreachable"
    assert status.last_error_code == "hub_not_configured"
    assert status.local_head is not None


def test_installing_the_manager_makes_a_plain_save_reach_the_hub(
    first: Device, hub: Path
) -> None:
    """The whole point of the port: a storage layer writes a file, knows
    nothing about Git, and the change ends up on the hub."""
    set_workspace_sync_port(first.manager)
    try:
        # The one thing the storage layer declares is its own write span.
        with shared_write_lock(first.root):
            change = write_shared_text(
                first.shared / CONFIG, "language: ja\n", workspace_root=first.root
            )
        assert change is not None
        first.manager.synchronize()

        assert first.manager.await_pushed(change.change_id) is True
    finally:
        set_workspace_sync_port(None)

    assert _hub_file(hub, CONFIG) == "language: ja\n"


def test_the_anomaly_carries_the_state_the_queue_stops_in() -> None:
    anomaly = SharedDataAnomaly("broken", "detail")
    ahead = SharedDataAnomaly("newer", "detail", state="update_required")

    assert anomaly.state == "invalid_shared_state"
    assert ahead.state == "update_required"
    assert str(anomaly) == "broken: detail"


def test_commits_name_the_device_and_the_time(first: Device, hub: Path) -> None:
    first.write(CONFIG, "language: ja\n")
    first.manager.synchronize()

    message = Repo(hub).head.commit.message
    assert "1 written, 0 deleted" in message
    assert "Device: device-mac" in message
    assert "Recorded-At:" in message


def test_the_hub_never_receives_device_local_data(first: Device, hub: Path) -> None:
    (first.shared / "local" / "run").mkdir(parents=True, exist_ok=True)
    (first.shared / "local" / "run" / "service.lock").write_text("1234")
    (first.shared / ".env").write_text("GITHUB_TOKEN=ghp-secret")
    first.write(CONFIG, "language: ja\n")

    first.manager.synchronize()

    tracked = Repo(hub).git.ls_tree("-r", "--name-only", "main").splitlines()
    assert not [path for path in tracked if path.startswith("local/")]
    assert ".env" not in tracked
