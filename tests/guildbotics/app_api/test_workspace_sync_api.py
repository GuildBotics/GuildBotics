"""The Desktop's hub and synchronization endpoints, against real repositories."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api import workspace_sync
from guildbotics.sync import current_sync_manager, deactivate_workspace_sync
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
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    root = tmp_path / "workspace"
    path = root / ".guildbotics" / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: demo\n", encoding="utf-8")
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
    deactivate_workspace_sync()


def _json(response) -> dict:
    assert response.status_code == HTTP_OK, response.text
    return response.json()


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


def test_retrying_an_unsynchronized_workspace_changes_nothing(
    client: TestClient,
) -> None:
    payload = _json(client.post("/workspace/sync/retry", headers=AUTH_HEADERS))

    assert payload["enabled"] is False


def test_retrying_a_synchronized_workspace_reports_its_state(
    client: TestClient,
) -> None:
    client.post("/hub", headers=AUTH_HEADERS)
    client.post("/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}})

    payload = _json(client.post("/workspace/sync/retry", headers=AUTH_HEADERS))

    assert payload["enabled"] is True
    assert payload["state"] == "idle"


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
