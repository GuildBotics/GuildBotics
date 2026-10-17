"""A member's Branvil account as a save and a deletion make it: the agent
GuildBotics creates or is given, the token it issues and keeps, the human
named after the owner, the GitHub credential and the picture it hands
Branvil, and the tokens it revokes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from guildbotics.integrations.branvil.setup import BranvilAccount
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.integrations.provider import (
    AppCredential,
    HostCredential,
    SavedMember,
    SetupError,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import KeyringSecretStore
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    MEMBER,
    OWNER,
    TOKEN,
    BranvilDouble,
    connect_admin,
    serve,
)

BRANVIL_SERVICES = {
    "code_hosting_service": {"name": "branvil", "base_url": BASE, "workspace": "a"},
    "ticket_service": {"name": "branvil", "base_url": BASE, "workspace": "a"},
}


def _member(
    person_id: str = "alice",
    human: bool = False,
    secrets: dict[str, str] | None = None,
    services: dict[str, Any] | None = None,
    **credentials: HostCredential,
) -> SavedMember:
    held = secrets or {}
    return SavedMember(
        person_id=person_id,
        name=person_id.title(),
        human=human,
        account_info={},
        get_secret=lambda name: held.get(name, ""),
        services=BRANVIL_SERVICES if services is None else services,
        credentials=credentials,
    )


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> BranvilDouble:
    double = BranvilDouble()
    serve(double, monkeypatch)
    connect_admin()
    return double


async def _saved(account: dict[str, Any], member: SavedMember) -> tuple[dict, dict]:
    form = BranvilAccount.model_validate(account)
    await form.prepare(member)
    return form.stored(member.person_id, member.human)


@pytest.mark.asyncio
async def test_a_new_agent_is_created_and_issued_a_token(double: BranvilDouble):
    info, secrets = await _saved({}, _member())

    assert double.members["alice"]["kind"] == "agent"
    assert double.members["alice"]["display_name"] == "Alice"
    assert info == {"branvil_username": "alice"}
    assert double.tokens[secrets["BRANVIL_TOKEN"]]["member"] == "alice"
    assert double.labels("alice") == ["guildbotics"]


@pytest.mark.asyncio
async def test_a_chosen_agent_is_issued_a_token_and_not_created(
    double: BranvilDouble,
):
    info, secrets = await _saved({"username": MEMBER}, _member())

    assert info == {"branvil_username": MEMBER}
    assert double.tokens[secrets["BRANVIL_TOKEN"]]["member"] == MEMBER
    assert f"POST /api/v1/members/{MEMBER}/tokens" in double.writes
    assert "POST /api/v1/members" not in double.writes


@pytest.mark.asyncio
async def test_a_token_branvil_takes_for_the_agents_is_kept(double: BranvilDouble):
    info, secrets = await _saved(
        {"username": MEMBER}, _member(secrets={"BRANVIL_TOKEN": TOKEN})
    )

    assert (info, secrets) == ({"branvil_username": MEMBER}, {})
    assert double.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "account"),
    [
        ("bvt_revoked", {"username": MEMBER}),
        # The token held is another member's: the agent was changed.
        ("bvt_aiko", {"username": "alice"}),
        (TOKEN, {"username": MEMBER, "reissue": True}),
    ],
    ids=["refused", "another_members", "reissue"],
)
async def test_a_token_is_issued_again_and_replaces_guildbotics_one_once_saved(
    double: BranvilDouble, held: str, account: dict[str, Any]
):
    name = account["username"]
    double.member("alice", "agent")
    earlier = double.issue(name, "guildbotics")["id"]
    form = BranvilAccount.model_validate(account)

    await form.prepare(_member(secrets={"BRANVIL_TOKEN": held}))
    _, secrets = form.stored("alice", False)
    issued = double.tokens[secrets["BRANVIL_TOKEN"]]

    assert issued["member"] == name
    # Until the save is done the token issued before keeps working.
    assert earlier in {t["id"] for t in double.tokens.values()}

    await form.saved()

    assert [t for t in double.tokens.values() if t["label"] == "guildbotics"] == [
        issued
    ]


@pytest.mark.asyncio
async def test_a_keychain_that_fails_after_the_save_fails_no_save(
    double: BranvilDouble, monkeypatch: pytest.MonkeyPatch
):
    earlier = double.issue(MEMBER, "guildbotics")["id"]
    form = BranvilAccount.model_validate({"username": MEMBER, "reissue": True})
    await form.prepare(_member(secrets={"BRANVIL_TOKEN": TOKEN}))

    def locked(self: KeyringSecretStore, key: str) -> str:
        raise SecretStoreError("keychain is locked")

    monkeypatch.setattr(KeyringSecretStore, "get", locked)

    await form.saved()

    # The earlier token keeps working until a save issues another.
    assert earlier in {t["id"] for t in double.tokens.values()}


@pytest.mark.asyncio
async def test_a_member_of_another_kind_is_not_taken_for_an_agent(
    double: BranvilDouble,
):
    with pytest.raises(SetupError) as raised:
        await _saved({"username": OWNER}, _member())

    assert raised.value.message == t("integrations.branvil.setup.not_agent", name=OWNER)
    assert double.writes == []


@pytest.mark.asyncio
async def test_a_human_is_named_after_the_owner(double: BranvilDouble):
    assert await _saved({}, _member(human=True)) == ({"branvil_username": OWNER}, {})
    # A name saved before is kept without asking Branvil.
    assert await _saved({"username": "boss"}, _member(human=True)) == (
        {"branvil_username": "boss"},
        {},
    )
    assert double.writes == []


@pytest.mark.asyncio
async def test_nothing_is_set_up_without_guildbotics_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    serve(BranvilDouble(), monkeypatch)

    with pytest.raises(SetupError) as raised:
        await _saved({}, _member())

    assert raised.value.code == "branvil"
    assert raised.value.message == t("integrations.branvil.api.not_connected")


@pytest.mark.asyncio
async def test_an_account_of_a_project_without_branvil_is_stored_as_given(
    double: BranvilDouble,
):
    member = _member(services={"chat_service": {"name": "slack"}})

    assert await _saved({"username": "aiko"}, member) == (
        {"branvil_username": "aiko"},
        {},
    )
    assert await _saved({}, member) == ({}, {})
    assert double.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential", "sent"),
    [
        (HostCredential(token="ghp_x"), {"token": "ghp_x"}),
        (
            HostCredential(
                app=AppCredential(app_id="3", installation_id="7", private_key="PEM")
            ),
            {"app": {"app_id": "3", "installation_id": "7", "private_key": "PEM"}},
        ),
    ],
    ids=["token", "app"],
)
async def test_the_agents_github_credential_is_handed_to_branvil(
    double: BranvilDouble, credential: HostCredential, sent: dict[str, Any]
):
    member = _member(secrets={"BRANVIL_TOKEN": TOKEN}, github=credential)

    await _saved({"username": MEMBER}, member)

    assert double.accounts == {(MEMBER, "github"): sent}


@pytest.mark.asyncio
@pytest.mark.parametrize("human", [False, True], ids=["agent", "owner"])
async def test_a_save_gives_the_branvil_member_the_members_picture(
    double: BranvilDouble, tmp_path, human: bool
):
    picture = tmp_path / "avatar.png"
    picture.write_bytes(b"\x89PNG picture")
    member = replace(
        _member(human=human, secrets={"BRANVIL_TOKEN": TOKEN}), avatar=picture
    )

    await _saved({"username": OWNER if human else MEMBER}, member)

    assert double.avatars == {OWNER if human else MEMBER: b"\x89PNG picture"}


@pytest.mark.asyncio
async def test_a_save_without_a_picture_puts_none(double: BranvilDouble):
    await _saved({"username": MEMBER}, _member(secrets={"BRANVIL_TOKEN": TOKEN}))

    assert double.avatars == {}
    assert not [w for w in double.writes if w.endswith("/avatar")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_info", "services", "given"),
    [
        ({"branvil_username": MEMBER}, None, {MEMBER: b"new"}),
        ({}, None, {}),
        ({"branvil_username": MEMBER}, {}, {}),
    ],
    ids=["with-account", "no-branvil-member", "no-branvil"],
)
async def test_a_changed_picture_is_given_to_the_branvil_member_the_account_names(
    double: BranvilDouble,
    tmp_path,
    account_info: dict[str, str],
    services: dict[str, Any] | None,
    given: dict[str, bytes],
):
    picture = tmp_path / "avatar.png"
    picture.write_bytes(b"new")
    member = replace(
        _member(services=services), account_info=account_info, avatar=picture
    )

    await BranvilAccount.avatar_changed(member)

    assert double.avatars == given


@pytest.mark.asyncio
async def test_deleting_an_agent_revokes_the_tokens_guildbotics_issued(
    double: BranvilDouble,
):
    double.issue(MEMBER, "guildbotics")
    member = replace(_member(), account_info={"branvil_username": MEMBER})

    await BranvilAccount.removed(member)

    assert double.labels(MEMBER) == ["cli"]
    assert MEMBER in double.members


@pytest.mark.asyncio
async def test_deleting_revokes_the_rest_when_one_was_revoked_meanwhile(
    double: BranvilDouble, monkeypatch: pytest.MonkeyPatch
):
    double.issue(MEMBER, "guildbotics")
    double.issue(MEMBER, "guildbotics")
    respond = double.respond
    raced: list[str] = []

    def revoked_meanwhile(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and not raced:
            raced.append(request.url.path)
            respond(request)
            gone = f"{BASE}/api/v1/members/nobody"
            return respond(httpx.Request("GET", gone, headers=request.headers))
        return respond(request)

    monkeypatch.setattr(double, "respond", revoked_meanwhile)

    await BranvilAccount.removed(
        replace(_member(), account_info={"branvil_username": MEMBER})
    )

    assert raced
    assert double.labels(MEMBER) == ["cli"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member",
    [
        SavedMember(
            person_id="otota",
            name="Otota",
            human=True,
            account_info={"branvil_username": OWNER},
            get_secret=lambda name: "",
            services=BRANVIL_SERVICES,
        ),
        _member(),
        SavedMember(
            person_id="ghost",
            name="Ghost",
            human=False,
            account_info={"branvil_username": "ghost"},
            get_secret=lambda name: "",
            services=BRANVIL_SERVICES,
        ),
    ],
    ids=["human", "no_branvil_name", "gone_from_branvil"],
)
async def test_deleting_revokes_nothing_of_a_member_without_an_agent(
    double: BranvilDouble, member: SavedMember
):
    await BranvilAccount.removed(member)

    assert double.labels(OWNER) == ["app:GuildBotics"]
    assert double.writes == []


GITHUB = PROVIDERS["github"].setup.member  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("info", "secrets", "credential"),
    [
        (
            {"github_account_type": "machine_user"},
            {"GITHUB_ACCESS_TOKEN": "ghp_x"},
            HostCredential(token="ghp_x"),
        ),
        (
            {
                "github_account_type": "github_apps",
                "github_app_id": "3",
                "github_installation_id": "7",
            },
            {"GITHUB_PRIVATE_KEY": "PEM"},
            HostCredential(
                app=AppCredential(app_id="3", installation_id="7", private_key="PEM")
            ),
        ),
        ({"github_account_type": "human"}, {"GITHUB_ACCESS_TOKEN": "ghp_x"}, None),
        ({"github_account_type": "machine_user"}, {}, None),
        ({"github_account_type": "github_apps"}, {}, None),
    ],
    ids=["machine_user", "github_apps", "human", "no_token", "no_key"],
)
def test_github_hands_over_a_machine_users_token_or_an_app(
    info: dict[str, str], secrets: dict[str, str], credential: HostCredential | None
):
    member = SavedMember(
        person_id="alice",
        name="Alice",
        human=False,
        account_info=info,
        get_secret=lambda name: secrets.get(name, ""),
        services={},
    )

    assert GITHUB.credential(member) == credential  # type: ignore[union-attr]
