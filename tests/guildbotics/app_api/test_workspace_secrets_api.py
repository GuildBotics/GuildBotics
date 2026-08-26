"""The Desktop's Secret endpoints, against a real hub on this machine.

Two things are asserted throughout: what the screen is told about each key, and
that no response, anywhere, carries a value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.sync import activation, current_sync_manager, deactivate_workspace_sync
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils.fileio import get_workspace_config_dir
from guildbotics.utils.secret_store import KeyringSecretStore
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port

HTTP_OK = 200
HTTP_CONFLICT = 409

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
TOKEN = "ghp-000111222333"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    root = tmp_path / "workspace"
    path = root / ".guildbotics" / "config" / "team" / "project.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: demo\n", encoding="utf-8")
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(root))
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def client(workspace: Path):
    del workspace
    client = TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )
    yield client
    if not deactivate_workspace_sync():
        manager = current_sync_manager()
        if manager is not None:
            assert GitSyncManager.stop(manager, timeout=10)
        activation._manager = None
        activation._workspace = None
    set_workspace_sync_port(None)


@pytest.fixture
def connected(client: TestClient) -> TestClient:
    """A workspace sharing with a hub this same machine hosts."""
    client.post("/hub", headers=AUTH_HEADERS)
    assert (
        client.post(
            "/workspace/sync/enable", headers=AUTH_HEADERS, json={"hub": {}}
        ).status_code
        == HTTP_OK
    )
    return client


def _json(response) -> dict:
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def _store() -> KeyringSecretStore:
    return KeyringSecretStore(get_workspace_config_dir())


def _state(payload: dict, key: str) -> dict:
    return next(entry for entry in payload["keys"] if entry["key"] == key)


def test_a_workspace_with_no_hub_has_nothing_to_transfer(client: TestClient) -> None:
    """The keys are still described -- they are this machine's own -- but there
    is no hub, so the screen shows none of the transfers."""
    _store().set("A_TOKEN", TOKEN)

    payload = _json(client.get("/workspace/secrets", headers=AUTH_HEADERS))

    assert payload["enabled"] is False
    assert payload["hub_reachable"] is False
    assert payload["hub_secret_store"] is None
    assert [entry["key"] for entry in payload["keys"]] == ["A_TOKEN"]


def test_transferring_without_a_hub_is_refused_plainly(client: TestClient) -> None:
    response = client.post("/workspace/secrets/send", headers=AUTH_HEADERS, json={})

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "workspace_sync_disabled"


def test_a_value_entered_here_is_waiting_to_be_sent(connected: TestClient) -> None:
    _store().set("A_TOKEN", TOKEN)

    payload = _json(connected.get("/workspace/secrets", headers=AUTH_HEADERS))

    assert _state(payload, "A_TOKEN")["status"] == "pending_send"
    assert _state(payload, "A_TOKEN")["hub_generation"] is None
    assert payload["pending_count"] == 1
    assert payload["attention_count"] == 1


def test_sending_publishes_the_generation_and_names_no_value(
    connected: TestClient,
) -> None:
    _store().set("A_TOKEN", TOKEN)

    response = connected.post(
        "/workspace/secrets/send", headers=AUTH_HEADERS, json={"keys": ["A_TOKEN"]}
    )

    payload = _json(response)
    assert payload["results"] == [{"key": "A_TOKEN", "status": "sent", "generation": 1}]
    assert _state(payload["secrets"], "A_TOKEN")["status"] == "ready"
    assert _state(payload["secrets"], "A_TOKEN")["hub_generation"] == 1
    assert TOKEN not in response.text


def test_sending_with_no_keys_sends_everything_waiting(connected: TestClient) -> None:
    store = _store()
    store.set("A_TOKEN", TOKEN)
    store.set("B_TOKEN", "xoxb-second")

    payload = _json(
        connected.post("/workspace/secrets/send", headers=AUTH_HEADERS, json={})
    )

    assert [result["key"] for result in payload["results"]] == ["A_TOKEN", "B_TOKEN"]
    assert payload["secrets"]["attention_count"] == 0


def test_a_device_that_holds_nothing_fetches_everything_in_one_call(
    connected: TestClient, workspace: Path
) -> None:
    """What a machine added to an existing workspace does: one action, no
    value retyped, and the values land in this machine's own secret store."""
    _store().set("A_TOKEN", TOKEN)
    connected.post("/workspace/secrets/send", headers=AUTH_HEADERS, json={})
    # This device forgets what it holds, the way a new machine never knew.
    (workspace / ".guildbotics" / "local" / "secrets.json").unlink()
    assert (
        _state(
            _json(connected.get("/workspace/secrets", headers=AUTH_HEADERS)), "A_TOKEN"
        )["status"]
        == "missing"
    )

    response = connected.post("/workspace/secrets/fetch", headers=AUTH_HEADERS, json={})

    payload = _json(response)
    assert payload["results"] == [
        {"key": "A_TOKEN", "status": "fetched", "generation": 1}
    ]
    assert _store().get("A_TOKEN") == TOKEN
    assert TOKEN not in response.text


def test_a_hub_generation_the_workspace_has_not_recorded_is_not_adopted(
    connected: TestClient, workspace: Path
) -> None:
    """The primary button on the screen must not resolve a send that was cut
    off by spreading the value the hub is holding.

    The state is visible because the hub's generation runs ahead of the one the
    shared history names -- nothing on this machine records it, so it reads the
    same way on every device and survives the value being entered again."""
    store = _store()
    store.set("A_TOKEN", TOKEN)
    connected.post("/workspace/secrets/send", headers=AUTH_HEADERS, json={})
    # A second send reaches the hub and its generation is never recorded.
    store.set("A_TOKEN", "ghp-rotated")
    connected.post(
        "/workspace/secrets/send", headers=AUTH_HEADERS, json={"keys": ["A_TOKEN"]}
    )
    _rewind_shared_generation(workspace, "A_TOKEN", 1)

    payload = _json(connected.get("/workspace/secrets", headers=AUTH_HEADERS))

    assert _state(payload, "A_TOKEN")["status"] == "unconfirmed"
    assert _state(payload, "A_TOKEN")["can_fetch"] is False
    assert payload["fetchable_keys"] == []

    refused = _json(
        connected.post(
            "/workspace/secrets/fetch", headers=AUTH_HEADERS, json={"keys": ["A_TOKEN"]}
        )
    )

    assert refused["results"] == [
        {"key": "A_TOKEN", "status": "generation_mismatch", "generation": 2}
    ]
    assert _state(refused["secrets"], "A_TOKEN")["status"] == "unconfirmed"


def test_a_send_settles_a_generation_no_one_recorded(
    connected: TestClient, workspace: Path
) -> None:
    """And the way out is the ordinary Send, from any machine holding a value."""
    store = _store()
    store.set("A_TOKEN", TOKEN)
    connected.post("/workspace/secrets/send", headers=AUTH_HEADERS, json={})
    store.set("A_TOKEN", "ghp-rotated")
    connected.post(
        "/workspace/secrets/send", headers=AUTH_HEADERS, json={"keys": ["A_TOKEN"]}
    )
    _rewind_shared_generation(workspace, "A_TOKEN", 1)

    settled = _json(
        connected.post(
            "/workspace/secrets/send", headers=AUTH_HEADERS, json={"keys": ["A_TOKEN"]}
        )
    )

    assert settled["results"] == [{"key": "A_TOKEN", "status": "sent", "generation": 3}]
    assert _state(settled["secrets"], "A_TOKEN")["status"] == "ready"


def _rewind_shared_generation(workspace: Path, key: str, generation: int) -> None:
    """Put the shared index back where an interrupted send would have left it.

    Synchronization delivers this file as a checkout rather than through a
    writer, so it is written directly here too."""
    from guildbotics.utils.fileio import dump_yaml, load_yaml_dict

    index_file = workspace / ".guildbotics" / "config" / "secrets.yml"
    index = load_yaml_dict(index_file)
    index["keys"][key]["generation"] = generation
    index_file.write_text(dump_yaml(index), encoding="utf-8")


def test_the_screen_is_told_which_transfer_each_key_can_take(
    connected: TestClient,
) -> None:
    _store().set("A_TOKEN", TOKEN)

    payload = _json(connected.get("/workspace/secrets", headers=AUTH_HEADERS))

    assert _state(payload, "A_TOKEN")["can_send"] is True
    assert _state(payload, "A_TOKEN")["can_fetch"] is False
    assert payload["sendable_keys"] == ["A_TOKEN"]
    assert payload["fetchable_keys"] == []


def test_the_response_names_which_machines_secret_store_answered(
    connected: TestClient,
) -> None:
    payload = _json(connected.get("/workspace/secrets", headers=AUTH_HEADERS))

    assert payload["hub_reachable"] is True
    assert payload["secret_store"] == {"available": True, "locked": False}
    assert payload["hub_secret_store"] == {"available": True, "locked": False}
