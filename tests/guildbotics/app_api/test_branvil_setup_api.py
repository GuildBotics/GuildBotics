"""Branvil's setup, as the setup screens and the browser drive it through
the App API against a Branvil double: connecting on the owner's consent,
the workspaces and agents the connection lists and creates, the entry into
Branvil's GUI, and the members whose saves and deletion set them up on
Branvil."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api import api
from guildbotics.integrations.branvil import setup as branvil_setup
from guildbotics.integrations.branvil.owner_git import git_config
from guildbotics.integrations.provider import page_path
from guildbotics.setup import setup_service
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.person_id import IDENTITY_FILE, identity_members_dir
from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store
from tests.guildbotics.app_api.owner import ORIGIN
from tests.guildbotics.app_api.provider_doubles import (
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
HTTP_CONFLICT = 409
HTTP_SERVER_ERROR = 500
OPERATIONS = "/config/providers/branvil"
SERVICES = {
    "code_hosting_service": {"name": "branvil", "workspace": "acme"},
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


def _store() -> KeyringSecretStore:
    """The identities' store: every workspace's members and connections."""
    return identity_secret_store()


def _operation(client: TestClient, operation: str, **body: Any) -> Any:
    response = client.post(
        f"{OPERATIONS}/{operation}",
        json={"base_url": BASE, **body},
    )
    assert response.status_code == HTTP_OK, response.text
    return response.json()


def _connect(client: TestClient) -> dict[str, Any]:
    return _operation(client, "connect")


def _return(client: TestClient, redirect: str) -> Any:
    """The browser, sent back by Branvil to this server as it reaches it, with
    no session cookie: SameSite=Strict keeps it from a navigation from another
    site."""
    assert redirect.startswith(f"{ORIGIN}{page_path('branvil', 'connected')}?")
    return TestClient(client.app).get(redirect)


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
        "base_url": "",
        "owner": "",
        "message": "",
        "git_config": "",
    }

    redirect = double.authorize(started["url"])
    page = _return(client, redirect)

    assert page.status_code == HTTP_OK
    assert t("integrations.branvil.setup.connected") in unescape(page.text)
    # A reload of the page exchanges the spent code no more.
    assert _return(client, redirect).status_code == HTTP_OK
    assert double.writes == ["POST /api/v1/apps/token"]
    admin = _store().get("BRANVIL_ADMIN_TOKEN")
    assert double.tokens[admin] == {
        "member": OWNER,
        "label": "app:GuildBotics",
        "id": f"tok_{admin}",
    }
    # The admin token issued before is replaced.
    assert ADMIN not in double.tokens
    assert _operation(client, "connection", state=started["state"]) == {
        "status": "connected",
        "base_url": BASE,
        "owner": OWNER,
        "message": "",
        "git_config": git_config(BASE),
    }


def test_an_admin_token_the_keychain_cannot_keep_asks_to_connect_again(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    started = _connect(client)

    def locked(self: KeyringSecretStore, key: str, value: str) -> None:
        raise SecretStoreError("keychain is locked")

    monkeypatch.setattr(KeyringSecretStore, "set", locked)

    page = _return(client, double.authorize(started["url"]))

    assert page.status_code == HTTP_BAD_REQUEST
    assert t("integrations.branvil.setup.not_kept") in unescape(page.text)
    assert _operation(client, "connection", state=started["state"])["message"] == (
        t("integrations.branvil.setup.not_kept")
    )


def test_connecting_to_where_no_branvil_answers_says_so_and_opens_nothing(
    client: TestClient, double: BranvilDouble, monkeypatch: pytest.MonkeyPatch
):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(double, "respond", refused)

    response = client.post(
        f"{OPERATIONS}/connect",
        json={"base_url": BASE},
    )

    assert response.status_code != HTTP_OK
    assert response.json()["message"] == t(
        "integrations.branvil.api.unreachable", base_url=BASE
    )


def test_a_denied_consent_connects_nothing(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)

    page = _return(client, double.authorize(started["url"], allow=False))

    assert page.status_code == HTTP_BAD_REQUEST
    assert t("integrations.branvil.setup.denied") in unescape(page.text)
    assert _store().get("BRANVIL_ADMIN_TOKEN") is None
    assert _operation(client, "connection", state=started["state"]) == {
        "status": "disconnected",
        "base_url": "",
        "owner": "",
        "message": t("integrations.branvil.setup.denied"),
        "git_config": "",
    }


def test_a_return_this_api_did_not_start_connects_nothing(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    started = _connect(client)
    redirect = double.authorize(started["url"])

    page = _return(client, redirect.replace(started["state"], "forged"))

    assert page.status_code == HTTP_BAD_REQUEST
    assert t("integrations.branvil.setup.expired") in unescape(page.text)
    assert _store().get("BRANVIL_ADMIN_TOKEN") is None


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
    assert _store().get("BRANVIL_ADMIN_TOKEN") is None
    connection = _operation(client, "connection", state=started["state"])
    assert connection["status"] == "disconnected"
    assert "400 invalid" in connection["message"]


def test_a_machine_without_the_connection_is_not_connected(client: TestClient):
    assert _operation(client, "connection") == {
        "status": "disconnected",
        "base_url": "",
        "owner": "",
        "message": "",
        "git_config": "",
    }


def test_a_connection_without_its_admin_token_is_not_connected(client: TestClient):
    connect_admin()
    _store().delete("BRANVIL_ADMIN_TOKEN")

    assert _operation(client, "connection") == {
        "status": "disconnected",
        "base_url": BASE,
        "owner": "",
        "message": t("integrations.branvil.api.not_connected"),
        "git_config": "",
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
    response = client.post(f"{OPERATIONS}/workspaces", json={"base_url": BASE})

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
    )


def _update(client: TestClient, workspace: Path, body: dict[str, Any]) -> Any:
    person_id = body["person_id"]
    read = client.get(f"/config/members/{person_id}").json()
    return client.put(
        f"/config/members/{person_id}",
        json={
            "config_dir": str(workspace / ".guildbotics/config"),
            "expected_revisions": read["revisions"],
            **body,
        },
    )


def _account_info(workspace: Path, person_id: str) -> dict[str, Any]:
    """The member's accounts, which its identity holds for every workspace."""
    identity = identity_members_dir() / person_id / IDENTITY_FILE
    return dict(load_yaml_file(identity)["account_info"])


def test_saving_an_agent_creates_it_on_branvil_and_keeps_its_token(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    assert _add(client, workspace, _member("alice")).status_code == HTTP_OK

    token = _store().get("ALICE_BRANVIL_TOKEN")
    assert double.tokens[token]["member"] == "alice"
    assert double.members["alice"]["kind"] == "agent"
    assert _account_info(workspace, "alice")["branvil_username"] == "alice"
    view = client.get("/config/members/alice").json()
    assert view["accounts"]["branvil"] == {"username": "alice", "has_token": True}

    # Saved again unchanged, the agent keeps the token Branvil takes.
    saved = _update(client, workspace, _member("alice", branvil={"username": "alice"}))
    assert saved.status_code == HTTP_OK
    assert _store().get("ALICE_BRANVIL_TOKEN") == token

    # A reissue replaces it.
    reissued = _update(
        client,
        workspace,
        _member("alice", branvil={"username": "alice", "reissue": True}),
    )
    assert reissued.status_code == HTTP_OK
    assert _store().get("ALICE_BRANVIL_TOKEN") not in (None, token)
    assert token not in double.tokens


def test_a_reissue_the_save_refuses_keeps_the_token_the_member_holds(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    token = _store().get("ALICE_BRANVIL_TOKEN")
    read = client.get("/config/members/alice").json()
    renamed = _update(client, workspace, {**_member("alice"), "person_name": "Ali"})
    assert renamed.status_code == HTTP_OK

    refused = client.put(
        "/config/members/alice",
        json={
            "config_dir": str(workspace / ".guildbotics/config"),
            "expected_revisions": read["revisions"],
            **_member("alice", branvil={"username": "alice", "reissue": True}),
        },
    )

    assert refused.status_code == HTTP_CONFLICT
    assert _store().get("ALICE_BRANVIL_TOKEN") == token
    # The token issued for the refused save is revoked; the one held is not.
    assert [v for v, t in double.tokens.items() if t["member"] == "alice"] == [token]


def test_a_save_failing_after_issuing_a_token_revokes_it(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    token = _store().get("ALICE_BRANVIL_TOKEN")
    respond = double.respond

    def no_accounts(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/github"):
            raise httpx.ConnectError("refused", request=request)
        return respond(request)

    monkeypatch.setattr(double, "respond", no_accounts)
    github = {"account_type": "machine_user", "username": "a", "access_token": "ghp_a"}

    failed = _update(
        client,
        workspace,
        _member("alice", branvil={"username": "alice", "reissue": True}, github=github),
    )

    assert failed.status_code == HTTP_BAD_REQUEST
    assert [v for v, t in double.tokens.items() if t["member"] == "alice"] == [token]


def test_a_save_failing_once_written_keeps_the_token_it_stored(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connect_admin()
    _add(client, workspace, _member("alice"))

    def unwritable(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    # The secrets are stored first, then the member's file is not.
    monkeypatch.setattr(setup_service, "save_yaml_file", unwritable)

    failed = _update(
        client,
        workspace,
        _member("alice", branvil={"username": "alice", "reissue": True}),
    )

    assert failed.status_code == HTTP_SERVER_ERROR
    stored = _store().get("ALICE_BRANVIL_TOKEN")
    assert double.tokens[stored]["member"] == "alice"


def test_a_save_settling_late_keeps_the_token_a_later_save_stored(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    first = _store().get("ALICE_BRANVIL_TOKEN")
    late: list[tuple[Any, bool]] = []
    settle = api.settle_accounts

    async def settling_later_once(member: Any, saved: bool) -> None:
        if late:
            await settle(member, saved)
        else:
            late.append((member, saved))

    monkeypatch.setattr(api, "settle_accounts", settling_later_once)
    reissue = _member("alice", branvil={"username": "alice", "reissue": True})

    # Save A is stored but not settled yet; save B reads the member A stored.
    assert _update(client, workspace, reissue).status_code == HTTP_OK
    assert _update(client, workspace, reissue).status_code == HTTP_OK
    kept = _store().get("ALICE_BRANVIL_TOKEN")
    asyncio.run(settle(*late[0]))

    assert [v for v, t in double.tokens.items() if t["member"] == "alice"] == [kept]
    assert first not in double.tokens


def test_a_save_stays_done_when_branvil_revokes_no_earlier_token(
    client: TestClient,
    double: BranvilDouble,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    token = _store().get("ALICE_BRANVIL_TOKEN")
    respond = double.respond

    def no_revoking(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ConnectError("refused", request=request)
        return respond(request)

    monkeypatch.setattr(double, "respond", no_revoking)

    saved = _update(
        client,
        workspace,
        _member("alice", branvil={"username": "alice", "reissue": True}),
    )

    assert saved.status_code == HTTP_OK
    kept = _store().get("ALICE_BRANVIL_TOKEN")
    assert {v for v, t in double.tokens.items() if t["member"] == "alice"} == {
        token,
        kept,
    }


def test_saving_an_agent_as_a_chosen_one_takes_its_name(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    body = _member("alice", branvil={"username": MEMBER})
    assert _add(client, workspace, body).status_code == HTTP_OK

    assert _account_info(workspace, "alice")["branvil_username"] == MEMBER
    token = _store().get("ALICE_BRANVIL_TOKEN")
    assert double.tokens[token]["member"] == MEMBER
    assert "alice" not in double.members


def test_a_human_takes_the_owners_name(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()

    assert _add(client, workspace, _member("otota", "human")).status_code == HTTP_OK

    assert _account_info(workspace, "otota")["branvil_username"] == OWNER
    assert _store().get("OTOTA_BRANVIL_TOKEN") is None
    assert double.writes == []


def test_a_member_branvil_refuses_is_not_saved(client: TestClient, workspace: Path):
    connect_admin()
    _store().delete("BRANVIL_ADMIN_TOKEN")

    response = _add(client, workspace, _member("alice"))

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["message"] == t("integrations.branvil.api.not_connected")
    assert not (workspace / ".guildbotics/config/team/members/alice").exists()
    assert not (identity_members_dir() / "alice").exists()


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
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()
    github = {
        "account_type": "github_apps",
        "username": "a[bot]",
        "app_id": 3,
        "installation_id": 7,
        "private_key": "PEM",
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

    response = client.delete("/identities/alice")

    assert response.status_code == HTTP_OK
    assert double.labels(MEMBER) == ["cli"]
    assert TOKEN in double.tokens
    assert not (identity_members_dir() / "alice").exists()
    assert not (workspace / ".guildbotics/config/team/members/alice").exists()


def test_taking_an_agent_out_of_a_workspace_keeps_its_token(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    """Other workspaces still have the member, and use the same token."""
    connect_admin()
    _add(client, workspace, _member("alice"))
    token = _store().get("ALICE_BRANVIL_TOKEN")

    response = client.delete("/config/members/alice")

    assert response.status_code == HTTP_OK
    assert not (workspace / ".guildbotics/config/team/members/alice").exists()
    assert (identity_members_dir() / "alice" / IDENTITY_FILE).is_file()
    assert double.tokens[token]["member"] == "alice"
    assert double.writes == [
        write for write in double.writes if not write.startswith("DELETE")
    ]


def test_an_agent_branvil_cannot_revoke_for_is_kept(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    connect_admin()
    _add(client, workspace, _member("alice"))
    _store().delete("BRANVIL_ADMIN_TOKEN")

    response = client.delete("/identities/alice")

    assert response.status_code == HTTP_BAD_REQUEST
    assert (workspace / ".guildbotics/config/team/members/alice").exists()
    assert (identity_members_dir() / "alice" / IDENTITY_FILE).is_file()
    assert double.labels("alice") == ["guildbotics"]


# -- several workspaces -------------------------------------------------------


def _open_workspace(client: TestClient, root: Path) -> Path:
    """Select another workspace using the same services, as a second project."""
    root.mkdir()
    selected = client.post("/workspace", json={"workspace_dir": str(root)})
    assert selected.status_code == HTTP_OK, selected.text
    config_dir = root / ".guildbotics/config"
    created = client.post(
        "/config/init",
        json={"config_dir": str(config_dir), "language": "en", "services": SERVICES},
    )
    assert created.status_code == HTTP_OK, created.text
    return root


def test_workspaces_on_one_branvil_revoke_none_of_each_others_tokens(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    """The connection and the member are the identities': one admin token and
    one agent token, whichever workspace connects, saves, or lets go."""
    started = _connect(client)
    _return(client, double.authorize(started["url"]))
    admin = _store().get("BRANVIL_ADMIN_TOKEN")
    assert _add(client, workspace, _member("alice")).status_code == HTTP_OK
    token = _store().get("ALICE_BRANVIL_TOKEN")
    other = _open_workspace(client, workspace / "other")

    # The other workspace is connected already, and has alice once added.
    assert _operation(client, "connection")["status"] == "connected"
    read = client.get("/config/members/alice").json()
    assert read["participating"] is False
    assert read["accounts"]["branvil"] == {"username": "alice", "has_token": True}
    joined = _add(
        client,
        other,
        {
            **_member("alice", branvil={"username": "alice"}),
            "expected_revisions": read["revisions"],
        },
    )
    assert joined.status_code == HTTP_OK, joined.text
    saved = _update(client, other, _member("alice", branvil={"username": "alice"}))
    assert saved.status_code == HTTP_OK
    left = client.delete("/config/members/alice")
    assert left.status_code == HTTP_OK

    assert _store().get("BRANVIL_ADMIN_TOKEN") == admin
    assert _store().get("ALICE_BRANVIL_TOKEN") == token
    assert double.tokens[admin]["label"] == "app:GuildBotics"
    assert double.tokens[token]["member"] == "alice"
    assert [v for v, t in double.tokens.items() if t["member"] == "alice"] == [token]
    assert (workspace / ".guildbotics/config/team/members/alice").is_dir()


def test_a_member_added_to_another_workspace_needs_no_reconnection(
    client: TestClient, double: BranvilDouble, workspace: Path
):
    """Adding the member to a workspace never connects again, so the consent
    that replaces the admin token is not asked of the owner twice."""
    connect_admin()
    _add(client, workspace, _member("alice"))
    other = _open_workspace(client, workspace / "other")
    read = client.get("/config/members/alice").json()

    joined = _add(
        client,
        other,
        {
            **_member("alice", branvil={"username": "alice"}),
            "expected_revisions": read["revisions"],
        },
    )

    assert joined.status_code == HTTP_OK
    assert "POST /api/v1/apps/token" not in double.writes
    assert ADMIN in double.tokens
