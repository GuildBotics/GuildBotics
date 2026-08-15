"""The Desktop's hub and synchronization endpoints, against real repositories."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.sync import deactivate_workspace_sync

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


def test_enabling_without_a_hub_reports_why(client: TestClient) -> None:
    """The hub has to exist before a workspace can be registered with it."""
    response = client.post(
        "/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "hub_register_failed"


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
