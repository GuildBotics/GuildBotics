"""The Desktop's hub and synchronization endpoints, against real repositories."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git import Repo

from guildbotics.app_api import workspace_sync
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.hub.host import hub_root
from guildbotics.runtime.live_state import LiveState
from guildbotics.sync import (
    activation,
    current_sync_manager,
    deactivate_workspace_sync,
    enrollment,
)
from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.sync.manager import GitSyncManager, GitSyncStatus
from guildbotics.sync.rejections import record_update_rejected
from guildbotics.utils import sync_lock as sync_lock_module
from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.live_freshness import LIVE_HEARTBEAT_INTERVAL_SECONDS
from guildbotics.utils.sync_lock import sync_lock_path
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port
from guildbotics.workspace.identity import read_workspace_identity

HTTP_OK = 200
HTTP_CONFLICT = 409
HTTP_BAD_REQUEST = 400

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
CONFIG = "config/team/project.yml"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace and a home directory of its own, both temporary."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    root = tmp_path / "workspace"
    path = root / ".guildbotics" / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"name: demo\n")
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(root))
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    client = TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )
    yield client
    if not deactivate_workspace_sync():
        # The test patched stop() to refuse, or the worker is mid-cycle; the
        # real worker must still stop, or it keeps cycling against later
        # tests. The slot is then released by hand.
        manager = current_sync_manager()
        if manager is not None:
            assert GitSyncManager.stop(manager, timeout=10), (
                "a synchronization worker outlived its test"
            )
        activation._manager = None
        activation._workspace = None
        set_workspace_sync_port(None)


def _json(response) -> dict:
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def test_expired_live_publisher_is_removed_from_the_service_cache() -> None:
    service = workspace_sync.WorkspaceSyncService()
    state = LiveState(
        workspace_id="0198ab00-0000-7000-8000-000000000001",
        device_id="0198ab00-0000-7000-8000-000000000002",
        publisher_id="0198ab00-0000-7000-8000-000000000003",
        observed_at="2026-08-23T00:00:00+00:00",
    )

    service._receive_live_state(state)
    assert [item.publisher_id for item in service.get_live_states()] == [
        state.publisher_id
    ]

    service._receive_live_expired(
        state.device_id, state.publisher_id, state.observed_at
    )

    assert service.get_live_states() == []


def test_listed_live_state_stays_online_when_the_publisher_clock_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    class FrozenDateTime:
        @staticmethod
        def now(tz=None):
            return viewer

    monkeypatch.setattr("guildbotics.utils.live_freshness.datetime", FrozenDateTime)
    service = workspace_sync.WorkspaceSyncService()
    state = LiveState(
        workspace_id="0198ab00-0000-7000-8000-000000000001",
        device_id="0198ab00-0000-7000-8000-000000000002",
        publisher_id="0198ab00-0000-7000-8000-000000000003",
        observed_at=(
            viewer - timedelta(seconds=LIVE_HEARTBEAT_INTERVAL_SECONDS + 6)
        ).isoformat(),
    )

    service._receive_live_state(state)

    assert service.get_live_states()[0].status == "online"


def test_desktop_owner_transfer_accepts_only_this_device(
    client: TestClient,
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})

    response = client.post(
        "/workspace/service-owner/transfer",
        headers=AUTH_HEADERS,
        json={"device_id": "0198ab00-0000-7000-8000-000000000002"},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "service_owner_target_invalid"


# -- Hosting a hub ------------------------------------------------------------


def test_a_machine_reports_that_it_hosts_no_hub(client: TestClient) -> None:
    payload = _json(client.get("/hub", headers=AUTH_HEADERS))

    assert payload["hosted"] is False
    assert payload["workspace_ids"] == []


def test_making_this_machine_a_hub_reports_an_address_to_share(
    client: TestClient,
) -> None:
    payload = _json(client.post("/hub", headers=AUTH_HEADERS))

    assert payload["hosted"] is True
    assert payload["ssh_endpoint"]
    assert (
        _json(client.get("/hub", headers=AUTH_HEADERS))["hub_id"] == payload["hub_id"]
    )


def test_a_local_hub_reports_the_workspaces_it_holds(client: TestClient) -> None:
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})

    payload = _json(client.post("/hub/inspect", headers=AUTH_HEADERS, json={}))

    assert payload["is_local"] is True
    assert payload["host_key_trusted"] is True
    assert len(payload["workspace_ids"]) == 1


def test_this_machine_is_not_offered_as_a_hub_unless_it_hosts_one(
    client: TestClient,
) -> None:
    """An empty address means the hub on this machine.

    A machine hosting none answers an empty workspace list just as a hub with
    nothing in it does, so without this the caller is led on to registering
    with a hub that is not there.
    """
    response = client.post("/hub/inspect", headers=AUTH_HEADERS, json={})

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "hub_not_hosted"


def test_an_address_that_names_nothing_is_refused(client: TestClient) -> None:
    response = client.post(
        "/hub/inspect", headers=AUTH_HEADERS, json={"endpoint": "not a host"}
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "invalid_hub_endpoint"


# -- Enabling synchronization -------------------------------------------------


def test_a_workspace_starts_out_unsynchronized(client: TestClient) -> None:
    payload = _json(client.get("/workspace/sync", headers=AUTH_HEADERS))

    assert payload["enabled"] is False
    assert payload["state"] == "disabled"
    assert payload["device_id"]


def test_enabling_registers_the_workspace_and_starts_the_queue(
    client: TestClient, workspace: Path
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)

    payload = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )

    assert payload["enabled"] is True
    assert payload["hub_url"]
    assert payload["ahead_count"] == 0
    assert payload["workspace_id"]


def test_enabling_without_a_hub_reports_why(
    client: TestClient, workspace: Path
) -> None:
    """The hub has to exist before a workspace can be registered with it.

    Refused while resolving the address rather than while registering: what
    follows has side effects, and registering mints this workspace's
    identifier -- which it then keeps for good -- before it can discover there
    is no hub to register with.
    """
    response = client.post(
        "/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "hub_not_hosted"
    assert read_workspace_identity(workspace) is None


def test_a_hub_that_does_not_hold_this_workspace_has_nothing_to_preview(
    client: TestClient,
) -> None:
    """The caller already knows it would be a registration, from the hub's own
    workspace list, and a preview must not make this workspace a repository to
    answer a question with one possible answer."""
    client.post("/hub", headers=AUTH_HEADERS)

    response = client.post(
        "/workspace/sync/preview", headers=AUTH_HEADERS, json={"hub": {}}
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "sync_preview_unavailable"
    assert (
        _json(client.get("/workspace/sync", headers=AUTH_HEADERS))["enabled"] is False
    )


def test_a_preview_before_a_first_connection_leaves_no_repository(
    client: TestClient, workspace: Path
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)

    client.post("/workspace/sync/preview", headers=AUTH_HEADERS, json={"hub": {}})

    assert not (workspace / ".guildbotics" / ".git").exists()
    assert not (workspace / ".guildbotics" / "state" / "workspace.json").exists()


def test_a_busy_sync_repository_answers_busy_and_keeps_the_queue(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview that outwaits sync.lock is a retryable answer, not a crash.

    Another process may hold the repository for longer than the wait limit;
    the user should read "try again", and the queue the pause stopped must be
    running again afterwards.
    """
    client.post("/hub", headers=AUTH_HEADERS)
    enabled = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )
    monkeypatch.setattr(sync_lock_module, "LOCK_TIMEOUT_SECONDS", 0.01)

    with held_lock(sync_lock_path(workspace)):
        response = client.post(
            "/workspace/sync/preview",
            headers=AUTH_HEADERS,
            json={"hub": {}, "workspace_id": enabled["workspace_id"]},
        )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "workspace_sync_busy"
    assert current_sync_manager() is not None


def test_retrying_an_unsynchronized_workspace_changes_nothing(
    client: TestClient,
) -> None:
    payload = _json(client.post("/workspace/sync/retry", headers=AUTH_HEADERS))

    assert payload["enabled"] is False


def test_retrying_a_synchronized_workspace_reports_its_state(
    client: TestClient,
) -> None:
    """Retrying a healthy workspace reports that attempt, with no error.

    The attempt runs a cycle under lock and answers with that cycle, so a
    successful retry is idle. The worker may start another cycle afterwards;
    that later state belongs to a subsequent status read, not this response.
    """
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})

    payload = _json(client.post("/workspace/sync/retry", headers=AUTH_HEADERS))

    assert payload["enabled"] is True
    assert payload["last_error_code"] is None
    assert payload["last_error_detail"] is None
    assert payload["state"] == "idle"


def test_a_hub_that_fails_reports_what_it_printed(
    client: TestClient, workspace: Path
) -> None:
    """The Desktop shows why the hub failed, in the words Git used.

    Retry answers with this attempt, so a missing hub is unreachable even if
    the worker has already started its next cycle when the response is sent.
    """
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    with activation.paused_workspace_sync(workspace):
        hub_root().rename(hub_root().with_name("gone"))

    payload = _json(client.post("/workspace/sync/retry", headers=AUTH_HEADERS))

    assert payload["state"] == "unreachable"
    assert payload["last_error_code"] == "HubCommandError"
    assert "fatal:" in payload["last_error_detail"]


@pytest.mark.parametrize(
    ("operation_name", "manager_method"),
    [("get_status", "status"), ("retry", "resume")],
)
def test_status_operations_keep_one_workspace_while_a_switch_starts(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
    manager_method: str,
) -> None:
    """Every response field belongs to the selected manager's workspace."""
    first_root = workspace
    hub = tmp_path / "hub.git"
    Repo.init(hub, bare=True, initial_branch="main")
    enrollment.enroll(str(hub), first_root)
    other_hub = tmp_path / "other-hub.git"
    Repo.init(other_hub, bare=True, initial_branch="main")
    second_root = tmp_path / "windows"
    second_root.mkdir()
    (second_root / ".guildbotics" / "state").mkdir(parents=True)
    (second_root / ".guildbotics" / CONFIG).parent.mkdir(parents=True, exist_ok=True)
    (second_root / ".guildbotics" / CONFIG).write_bytes(b"name: other\n")
    enrollment.enroll(str(other_hub), second_root)

    first = activation.activate_workspace_sync(first_root)
    assert first is not None
    first_identity = read_workspace_identity(first_root)
    second_identity = read_workspace_identity(second_root)
    assert first_identity is not None
    assert second_identity is not None
    rejection_id = _reject(first_root)
    record_update_rejected(
        rejection_id=rejection_id,
        paths=["config/team/other.yml"],
        device_id="device-windows",
        workspace_id=second_identity.workspace_id,
        workspace_root=second_root,
    )

    service = workspace_sync.WorkspaceSyncService()
    service._live_error_code = "relay_failed"
    inside = threading.Event()
    release = threading.Event()
    real_method = getattr(first, manager_method)

    def delayed_manager_operation() -> GitSyncStatus:
        inside.set()
        assert release.wait(5)
        return real_method()

    monkeypatch.setattr(first, manager_method, delayed_manager_operation)
    result: list[workspace_sync.WorkspaceSyncStatus] = []

    def read_status() -> None:
        result.append(getattr(service, operation_name)())

    reader = threading.Thread(target=read_status)
    reader.start()
    assert inside.wait(5)

    def switch() -> None:
        monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(second_root))
        service.deactivate()
        activation.activate_workspace_sync(second_root)

    switcher = threading.Thread(target=switch)
    switcher.start()
    switcher.join(0.2)
    assert switcher.is_alive(), "a workspace switch entered during a status operation"
    release.set()
    reader.join(5)
    switcher.join(5)

    payload = result[0]
    assert payload.workspace_id == first_identity.workspace_id
    assert payload.rejected_changes[0].rejection_id == rejection_id
    assert payload.rejected_changes[0].paths == [CONFIG]
    assert payload.live_error_code == "relay_failed"
    assert LocalSyncRepository(first_root).remote_url() == payload.hub_url


# -- Taking a workspace from a hub --------------------------------------------


def test_a_copy_becomes_the_selected_workspace(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)
    enabled = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )
    destination = tmp_path / "second"

    payload = _json(
        client.post(
            "/workspace/sync/clone",
            headers=AUTH_HEADERS,
            json={
                "hub": {},
                "workspace_id": enabled["workspace_id"],
                "workspace_dir": str(destination),
            },
        )
    )

    assert Path(payload["workspace"]) == destination
    assert (destination / ".guildbotics" / CONFIG).read_text() == "name: demo\n"


def test_a_copy_lands_in_a_folder_this_device_has_only_opened(
    client: TestClient, tmp_path: Path
) -> None:
    """The Desktop takes the destination from the workspace field, and setting
    that field opens the folder -- which writes this device's diagnostics under
    ``local/``. Refusing on the directory rather than on its content made the
    screen's own way of naming a destination the one that could not work."""
    client.post("/hub", headers=AUTH_HEADERS)
    enabled = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )
    destination = tmp_path / "second"
    scratch = destination / ".guildbotics" / "local" / "run"
    scratch.mkdir(parents=True)
    (scratch / "diagnostics.jsonl").write_text("{}\n", encoding="utf-8")

    payload = _json(
        client.post(
            "/workspace/sync/clone",
            headers=AUTH_HEADERS,
            json={
                "hub": {},
                "workspace_id": enabled["workspace_id"],
                "workspace_dir": str(destination),
            },
        )
    )

    assert Path(payload["workspace"]) == destination
    assert (destination / ".guildbotics" / CONFIG).read_text() == "name: demo\n"


def test_a_copy_refuses_a_directory_that_already_holds_a_workspace(
    client: TestClient, workspace: Path
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)
    enabled = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )

    response = client.post(
        "/workspace/sync/clone",
        headers=AUTH_HEADERS,
        json={
            "hub": {},
            "workspace_id": enabled["workspace_id"],
            "workspace_dir": str(workspace),
        },
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "workspace_already_exists"


# -- What the hub did not accept ----------------------------------------------


def _reject(workspace: Path) -> str:
    """Displace one local commit, the way a lost race does, and record it."""
    from guildbotics.sync import LocalSyncRepository, record_update_rejected

    rejection_id = "01a01500-0000-7000-8000-00000000000a"
    repository = LocalSyncRepository(workspace)
    head = repository.head() or ""
    repository.save_rejected(rejection_id, head)
    record_update_rejected(
        rejection_id=rejection_id,
        paths=[CONFIG],
        device_id="1f0a0000-0000-7000-8000-0000000000d1",
        workspace_id="1f0a0000-0000-7000-8000-00000000000a",
        workspace_root=workspace,
    )
    return rejection_id


def test_the_displaced_commits_this_device_holds_are_reported(
    client: TestClient, workspace: Path
) -> None:
    """The warning has to be able to end, and the refs are what says whether it
    should: nothing deletes them on its own, so a device holding none is a
    device with nothing left for the user to look at."""
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    rejection_id = _reject(workspace)

    payload = _json(client.get("/workspace/sync", headers=AUTH_HEADERS))

    held = payload["rejected_changes"]
    assert [item["rejection_id"] for item in held] == [rejection_id]
    # The files come from what was recorded when the change was displaced. The
    # commit cannot answer it: a workspace that has just joined a hub has one
    # commit, holding everything it owns rather than the few paths that lost.
    assert held[0]["paths"] == [CONFIG]
    assert held[0]["occurred_at"]


def test_the_user_can_say_they_are_done_with_a_displaced_commit(
    client: TestClient, workspace: Path
) -> None:
    """Discarding is the one thing the screen can offer without reading the
    content, and the only way the warning ends: the recovery procedure itself
    leaves the ref in place on purpose."""
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    rejection_id = _reject(workspace)

    payload = _json(
        client.post(
            f"/workspace/sync/rejections/{rejection_id}/discard", headers=AUTH_HEADERS
        )
    )

    assert payload["rejected_changes"] == []


def test_a_rejection_id_that_names_no_ref_is_refused(
    client: TestClient, workspace: Path
) -> None:
    """The identifier reaches Git as part of a ref name, so anything that is
    not one of ours is turned away before it gets there."""
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})

    response = client.post(
        "/workspace/sync/rejections/heads-main/discard", headers=AUTH_HEADERS
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "sync_discard_failed"


# -- Registering this device with a hub ---------------------------------------


def test_this_device_reports_no_key_before_one_is_made(client: TestClient) -> None:
    payload = _json(client.get("/hub/ssh-key", headers=AUTH_HEADERS))

    assert payload["exists"] is False
    assert payload["public_key"] == ""


def test_a_hub_that_cannot_be_reached_is_an_answer_not_a_crash(
    client: TestClient, tmp_path: Path
) -> None:
    """A key not registered yet, a hub that is off, a wrong address: these are
    the normal way this fails, so the Desktop has to be able to show them."""
    client.post("/hub", headers=AUTH_HEADERS)
    enabled = _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )

    response = client.post(
        "/workspace/sync/clone",
        headers=AUTH_HEADERS,
        json={
            "hub": {"endpoint": "hub.invalid"},
            "workspace_id": enabled["workspace_id"],
            "workspace_dir": str(tmp_path / "second"),
        },
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "sync_clone_failed"


def test_a_workspace_identifier_that_is_not_one_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)

    response = client.post(
        "/workspace/sync/clone",
        headers=AUTH_HEADERS,
        json={
            "hub": {},
            "workspace_id": "../../etc",
            "workspace_dir": str(tmp_path / "second"),
        },
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "sync_clone_failed"


# -- The queue and the enrollment work never share the repository -------------


def _enabled(client: TestClient) -> dict:
    client.post("/hub", headers=AUTH_HEADERS)
    return _json(
        client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})
    )


def test_changing_the_hub_stops_the_queue_before_it_touches_the_repository(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrolling commits, fetches, and resets the branch the running queue is
    working in, so the two must never be in there together."""
    enabled = _enabled(client)
    running: list[bool] = []
    real_enroll = workspace_sync.enroll
    monkeypatch.setattr(
        workspace_sync,
        "enroll",
        lambda *args, **kwargs: (
            running.append(current_sync_manager() is not None),
            real_enroll(*args, **kwargs),
        )[1],
    )

    client.post(
        "/workspace/sync/hub",
        headers=AUTH_HEADERS,
        json={"hub": {}, "workspace_id": enabled["workspace_id"]},
    )

    assert running == [False]


def test_the_queue_is_running_again_after_a_failed_hub_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt must not leave a workspace that has a hub quietly not
    synchronizing. The failure has to happen inside the pause to prove it: a
    request rejected before the queue stops would pass without testing anything.
    """
    enabled = _enabled(client)
    paused: list[bool] = []

    def failing_enroll(*args: object, **kwargs: object) -> None:
        paused.append(current_sync_manager() is None)
        raise workspace_sync.EnrollmentError("the hub refused this workspace")

    monkeypatch.setattr(workspace_sync, "enroll", failing_enroll)

    response = client.post(
        "/workspace/sync/hub",
        headers=AUTH_HEADERS,
        json={"hub": {}, "workspace_id": enabled["workspace_id"]},
    )

    assert response.status_code == HTTP_CONFLICT
    assert paused == [True], "the failure did not happen inside the pause"
    assert current_sync_manager() is not None
    assert _json(client.get("/workspace/sync", headers=AUTH_HEADERS))["enabled"] is True


def test_a_queue_that_will_not_stop_blocks_the_hub_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = _enabled(client)
    manager = current_sync_manager()
    assert manager is not None
    monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)

    response = client.post(
        "/workspace/sync/hub",
        headers=AUTH_HEADERS,
        json={"hub": {}, "workspace_id": enabled["workspace_id"]},
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "workspace_sync_busy"


# -- Trusting a hub -----------------------------------------------------------


def test_trusting_a_hub_requires_the_confirmed_fingerprint(
    client: TestClient,
) -> None:
    response = client.post(
        "/hub/trust", headers=AUTH_HEADERS, json={"endpoint": "hub.local"}
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "host_key_not_confirmed"


def test_a_hub_offering_a_different_key_asks_the_user_to_look_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the answer is an unhandled error rather than the one thing the
    user can act on: check the fingerprint again."""

    def changed(endpoint: object, fingerprint: str) -> None:
        raise workspace_sync.HostKeyChangedError("hub.local offers another key")

    monkeypatch.setattr(workspace_sync.connection, "trust_host_key", changed)

    response = client.post(
        "/hub/trust",
        headers=AUTH_HEADERS,
        json={"endpoint": "hub.local", "fingerprint": "SHA256:confirmed"},
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "host_key_changed"


# -- Devices sharing the workspace --------------------------------------------


def test_the_device_list_is_empty_until_this_machine_joins_one(
    client: TestClient,
) -> None:
    """A workspace with no hub publishes no device record, so there is nothing
    to list -- not even this machine."""
    payload = _json(client.get("/workspace/devices", headers=AUTH_HEADERS))

    assert payload["devices"] == []
    assert payload["device_id"]


def test_this_machine_appears_once_it_has_a_record(
    client: TestClient, workspace: Path
) -> None:
    _json(client.post("/hub", headers=AUTH_HEADERS))
    _json(client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}))

    payload = _json(client.get("/workspace/devices", headers=AUTH_HEADERS))

    assert [device["is_self"] for device in payload["devices"]] == [True]
    assert payload["devices"][0]["device_id"] == payload["device_id"]
    assert payload["devices"][0]["os"]
    assert payload["devices"][0]["joined_at"]


def test_activation_republishes_the_current_device_key_fingerprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_sync, "_key_fingerprint", lambda: None)
    _json(client.post("/hub", headers=AUTH_HEADERS))
    _json(client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}))
    assert (
        _json(client.get("/workspace/devices", headers=AUTH_HEADERS))["devices"][0][
            "ssh_public_key_fingerprint"
        ]
        == ""
    )

    monkeypatch.setattr(
        workspace_sync, "_key_fingerprint", lambda: "SHA256:current-device"
    )
    workspace_sync.WorkspaceSyncService().activate()

    devices = _json(client.get("/workspace/devices", headers=AUTH_HEADERS))["devices"]
    assert devices[0]["ssh_public_key_fingerprint"] == "SHA256:current-device"


def test_renaming_this_machine_publishes_the_new_name(
    client: TestClient, workspace: Path
) -> None:
    _json(client.post("/hub", headers=AUTH_HEADERS))
    _json(client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}))

    payload = _json(
        client.post(
            "/workspace/devices/self",
            headers=AUTH_HEADERS,
            json={"display_name": "  Work laptop  "},
        )
    )

    assert [device["display_name"] for device in payload["devices"]] == ["Work laptop"]
    stored = _json(client.get("/workspace/devices", headers=AUTH_HEADERS))
    assert stored["devices"][0]["display_name"] == "Work laptop"


def test_a_blank_device_name_is_refused(client: TestClient) -> None:
    response = client.post(
        "/workspace/devices/self", headers=AUTH_HEADERS, json={"display_name": "   "}
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "device_name_invalid"
