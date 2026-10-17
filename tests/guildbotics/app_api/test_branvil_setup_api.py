"""Branvil's setup, as the setup screens and the browser drive it through
the App API against a Branvil double: connecting on the owner's consent,
the workspaces and agents the connection lists and creates, the entry into
Branvil's GUI, and the members whose saves and deletion set them up on
Branvil."""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from guildbotics.integrations.branvil import setup as branvil_setup
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.secret_store import KeyringSecretStore
from tests.guildbotics.app_api.provider_doubles import (
    AUTH_HEADERS,
    saved_workspace,
)
from tests.guildbotics.integrations.branvil_double import (
    ADMIN,
    BASE,
    MEMBER,
    OWNER,
    TOKEN,
    BranvilDouble,
    connect_admin,
    serve,
)

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
CALLBACK_BASE = "http://127.0.0.1:5173"
OPERATIONS = "/config/providers/branvil"
SERVICES = {
    "code_hosting_service": {"name": "branvil", "base_url": BASE, "workspace": "acme"},
    "ticket_service": {"name": "branvil", "workspace": "acme"},
}


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> BranvilDouble:
    # Consents live in the process; each test starts with none.
    monkeypatch.setattr(branvil_setup.consents, "_held", {})
    double = BranvilDouble()
    serve(double, monkeypatch)
    return double


@pytest.fixture
def client(workspace: Path, double: BranvilDouble) -> Iterator[TestClient]:
    with saved_workspace(workspace, SERVICES) as client:
        yield client


def _store(workspace: Path) -> KeyringSecretStore:
    return KeyringSecretStore(workspace / ".guildbotics/config")


def _operation(client: TestClient, operation: str, **body: Any) -> Any:
    response = client.post(
        f"{OPERATIONS}/{operation}",
        json={"base_url": BASE, **body},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def _connect(client: TestClient) -> dict[str, Any]:
    return _operation(client, "connect", callback_base_url=CALLBACK_BASE)


def _return(client: TestClient, redirect: str) -> Any:
    """The browser, sent back by Branvil to this local API."""
    return client.get(redirect.removeprefix(CALLBACK_BASE))


# -- the connection -----------------------------------------------------------


def test_connecting_keeps_the_admin_token_branvil_issues_on_consent(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)
    url = urlsplit(started["url"])
    assert (url.scheme, url.netloc, url.path) == (
        "http",
        "127.0.0.1:7878",
        "/-/authorize",
    )
    assert _operation(client, "connection", state=started["state"]) == {
        "status": "pending",
        "owner": "",
        "message": "",
    }

    redirect = double.authorize(started["url"])
    page = _return(client, redirect)

    assert page.status_code == HTTP_OK
    assert t("integrations.branvil.setup.connected") in unescape(page.text)
    # A reload of the page exchanges the spent code no more.
    assert _return(client, redirect).status_code == HTTP_OK
    assert double.writes == ["POST /api/v1/apps/token"]
    admin = _store(workspace).get("BRANVIL_ADMIN_TOKEN")
    assert double.tokens[admin] == {
        "member": OWNER,
        "label": "app:GuildBotics",
        "id": f"tok_{admin}",
    }
    # The admin token issued before is replaced.
    assert ADMIN not in double.tokens
    assert _operation(client, "connection", state=started["state"]) == {
        "status": "connected",
        "owner": OWNER,
        "message": "",
    }


def test_a_denied_consent_connects_nothing(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)

    page = _return(client, double.authorize(started["url"], allow=False))

    assert page.status_code == HTTP_BAD_REQUEST
    assert t("integrations.branvil.setup.denied") in unescape(page.text)
    assert _store(workspace).get("BRANVIL_ADMIN_TOKEN") is None
    assert _operation(client, "connection", state=started["state"]) == {
        "status": "disconnected",
        "owner": "",
        "message": t("integrations.branvil.setup.denied"),
    }


def test_a_return_this_api_did_not_start_connects_nothing(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)
    redirect = double.authorize(started["url"])

    page = _return(client, redirect.replace(started["state"], "forged"))

    assert page.status_code == HTTP_BAD_REQUEST
    assert t("integrations.branvil.setup.expired") in unescape(page.text)
    assert _store(workspace).get("BRANVIL_ADMIN_TOKEN") is None


def test_a_code_branvil_refuses_connects_nothing(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)
    redirect = double.authorize(started["url"])
    # The code was spent elsewhere: Branvil refuses it at the exchange.
    double._codes.clear()

    page = _return(client, redirect)

    assert page.status_code == HTTP_BAD_REQUEST
    assert "400 invalid" in unescape(page.text)
    assert _store(workspace).get("BRANVIL_ADMIN_TOKEN") is None
    connection = _operation(client, "connection", state=started["state"])
    assert connection["status"] == "disconnected"
    assert "400 invalid" in connection["message"]


def test_a_machine_without_the_admin_token_is_not_connected(client: TestClient):
    assert _operation(client, "connection") == {
        "status": "disconnected",
        "owner": "",
        "message": t("integrations.branvil.api.not_connected"),
    }


# -- what the connection administers ------------------------------------------


def test_the_workspaces_are_listed_and_created(
    client: TestClient, double: BranvilDouble
):
    connect_admin()

    assert _operation(client, "workspaces") == {"workspaces": ["acme", "other"]}
    assert _operation(client, "workspaces/create", name="beta") == {
        "workspaces": ["acme", "other", "beta"]
    }
    refused = client.post(
        f"{OPERATIONS}/workspaces/create",
        json={"base_url": BASE, "name": "acme"},
        headers=AUTH_HEADERS,
    )
    assert refused.status_code == HTTP_BAD_REQUEST
    assert refused.json()["code"] == "branvil"


def test_the_agents_are_listed_for_a_member_to_be(client: TestClient):
    connect_admin()

    assert _operation(client, "agents") == {"agents": [MEMBER]}


def test_branvils_gui_is_entered_by_a_link_the_admin_token_asks_for(
    client: TestClient, double: BranvilDouble
):
    connect_admin()

    url = _operation(client, "entry")["url"]

    assert url.startswith(f"{BASE}/-/enter#")
    assert double.writes == ["POST /api/v1/session/links"]


def test_nothing_is_administered_without_the_connection(client: TestClient):
    response = client.post(
        f"{OPERATIONS}/workspaces", json={"base_url": BASE}, headers=AUTH_HEADERS
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "branvil",
        "message": t("integrations.branvil.api.not_connected"),
        "context": {},
    }


# -- the members --------------------------------------------------------------


def _member(person_id: str, person_type: str = "", **accounts: Any) -> dict:
    return {
        "person_type": person_type,
        "person_id": person_id,
        "person_name": person_id.title(),
        "is_active": person_type != "human",
        "accounts": {"branvil": {}, **accounts},
    }


def _add(client: TestClient, workspace: Path, body: dict[str, Any]) -> Any:
    return client.post(
        "/config/members",
        json={"config_dir": str(workspace / ".guildbotics/config"), **body},
        headers=AUTH_HEADERS,
    )


def _update(client: TestClient, workspace: Path, body: dict[str, Any]) -> Any:
    person_id = body["person_id"]
    read = client.get(f"/config/members/{person_id}", headers=AUTH_HEADERS).json()
    return client.put(
        f"/config/members/{person_id}",
        json={
            "config_dir": str(workspace / ".guildbotics/config"),
            "original_person_id": person_id,
            "expected_revisions": read["revisions"],
            **body,
        },
        headers=AUTH_HEADERS,
    )


def _account_info(workspace: Path, person_id: str) -> dict[str, Any]:
    person = workspace / f".guildbotics/config/team/members/{person_id}/person.yml"
    return dict(load_yaml_file(person)["account_info"])


def test_saving_an_agent_creates_it_on_branvil_and_keeps_its_token(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    assert _add(client, workspace, _member("alice")).status_code == HTTP_OK

    token = _store(workspace).get("ALICE_BRANVIL_TOKEN")
    assert double.tokens[token]["member"] == "alice"
    assert double.members["alice"]["kind"] == "agent"
    assert _account_info(workspace, "alice")["branvil_username"] == "alice"
    view = client.get("/config/members/alice", headers=AUTH_HEADERS).json()
    assert view["accounts"]["branvil"] == {"username": "alice", "has_token": True}

    # Saved again unchanged, the agent keeps the token Branvil takes.
    saved = _update(client, workspace, _member("alice", branvil={"username": "alice"}))
    assert saved.status_code == HTTP_OK
    assert _store(workspace).get("ALICE_BRANVIL_TOKEN") == token

    # A reissue replaces it.
    reissued = _update(
        client,
        workspace,
        _member("alice", branvil={"username": "alice", "reissue": True}),
    )
    assert reissued.status_code == HTTP_OK
    assert _store(workspace).get("ALICE_BRANVIL_TOKEN") not in (None, token)
    assert token not in double.tokens


def test_saving_an_agent_as_a_chosen_one_takes_its_name(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    body = _member("alice", branvil={"username": MEMBER})
    assert _add(client, workspace, body).status_code == HTTP_OK

    assert _account_info(workspace, "alice")["branvil_username"] == MEMBER
    token = _store(workspace).get("ALICE_BRANVIL_TOKEN")
    assert double.tokens[token]["member"] == MEMBER
    assert "alice" not in double.members


def test_a_human_takes_the_owners_name(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    assert _add(client, workspace, _member("otota", "human")).status_code == HTTP_OK

    assert _account_info(workspace, "otota")["branvil_username"] == OWNER
    assert _store(workspace).get("OTOTA_BRANVIL_TOKEN") is None
    assert double.writes == []


def test_a_member_branvil_refuses_is_not_saved(client: TestClient, workspace: Path):
    response = _add(client, workspace, _member("alice"))

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["message"] == t("integrations.branvil.api.not_connected")
    assert not (workspace / ".guildbotics/config/team/members/alice").exists()


@pytest.mark.parametrize(
    ("github", "sent"),
    [
        (
            {"account_type": "machine_user", "username": "a", "access_token": "ghp_a"},
            {"token": "ghp_a"},
        ),
        ({"account_type": "human", "username": "a", "access_token": "ghp_a"}, None),
    ],
    ids=["machine_user", "human_account"],
)
def test_an_agents_github_credential_is_handed_to_branvil_on_every_save(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    github: dict[str, Any],
    sent: dict[str, Any] | None,
):
    connect_admin()

    assert _add(client, workspace, _member("alice", github=github)).status_code == (
        HTTP_OK
    )
    assert double.accounts.get(("alice", "github")) == sent
    double.accounts.clear()

    # The token kept in the keychain is handed over again.
    github = {**github, "access_token": ""}
    body = _member("alice", branvil={"username": "alice"}, github=github)
    assert _update(client, workspace, body).status_code == HTTP_OK
    assert double.accounts.get(("alice", "github")) == sent


def test_an_agents_github_app_is_handed_to_branvil(
    client: TestClient, double: BranvilDouble, workspace: Path, tmp_path: Path
):
    connect_admin()
    key = tmp_path / "app.pem"
    key.write_text("PEM", encoding="utf-8")
    github = {
        "account_type": "github_apps",
        "username": "a[bot]",
        "app_id": 3,
        "installation_id": 7,
        "private_key_path": str(key),
    }

    assert _add(client, workspace, _member("alice", github=github)).status_code == (
        HTTP_OK
    )

    assert double.accounts[("alice", "github")] == {
        "app": {"app_id": "3", "installation_id": "7", "private_key": "PEM"}
    }


def test_deleting_an_agent_revokes_its_tokens_on_branvil(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()
    _add(client, workspace, _member("alice", branvil={"username": MEMBER}))

    response = client.request(
        "DELETE",
        "/config/members/alice",
        json={"config_dir": str(workspace / ".guildbotics/config")},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_OK
    assert double.labels(MEMBER) == ["cli"]
    assert TOKEN in double.tokens


def test_an_agent_branvil_cannot_revoke_for_is_kept(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    _store(workspace).delete("BRANVIL_ADMIN_TOKEN")

    response = client.request(
        "DELETE",
        "/config/members/alice",
        json={"config_dir": str(workspace / ".guildbotics/config")},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert (workspace / ".guildbotics/config/team/members/alice").exists()
    assert double.labels("alice") == ["guildbotics"]
