"""The owner's own git on a machine: the token the machine issues the owner
with GuildBotics' connection, keeps in its keychain, hands git while Branvil
takes it, and issues again once Branvil does not."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import keyring
import pytest

from guildbotics.integrations.branvil import owner_git
from guildbotics.integrations.branvil.owner_git import git_config, owner_git_credential
from guildbotics.integrations.factory import human_git_credential
from guildbotics.runtime.port import ServiceError
from guildbotics.utils import managed_cli
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import identity_secret_store
from guildbotics.workspace.identity import (
    ensure_device_identity,
    set_device_display_name,
)
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    MEMBER,
    OWNER,
    TOKEN,
    BranvilDouble,
    connect_admin,
    serve,
)

_TOKENS = f"POST /api/v1/members/{OWNER}/tokens"


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> BranvilDouble:
    double = BranvilDouble()
    serve(double, monkeypatch)
    connect_admin()
    return double


def _held() -> str | None:
    return keyring.get_password("GuildBotics/branvil-git", BASE)


def _machine_tokens(double: BranvilDouble) -> list[str]:
    return [
        token
        for token, issued in double.tokens.items()
        if issued["member"] == OWNER and issued["label"].startswith("git:")
    ]


@pytest.mark.asyncio
async def test_the_first_request_issues_the_machine_its_own_owner_token(
    double: BranvilDouble,
):
    credential = await human_git_credential(BASE)

    assert credential is not None
    assert credential.username == OWNER
    assert _held() == credential.password
    device = ensure_device_identity()
    assert double.tokens[credential.password]["label"] == (
        f"git:{device.display_name} ({device.device_id[-8:]})"
    )
    assert double.writes == [_TOKENS]
    # Not one of the identities' Secrets, which the Hub hands every machine.
    assert credential.password not in identity_secret_store().values().values()


@pytest.mark.asyncio
async def test_the_token_branvil_takes_is_handed_again(double: BranvilDouble):
    first = await owner_git_credential(BASE)

    assert await owner_git_credential(BASE) == first
    assert double.writes == [_TOKENS]


@pytest.mark.asyncio
async def test_a_revoked_token_is_issued_again_in_its_place(double: BranvilDouble):
    first = await owner_git_credential(BASE)
    assert first is not None
    del double.tokens[first.password]

    second = await owner_git_credential(BASE)

    assert second is not None
    assert second.password != first.password
    assert _held() == second.password
    assert _machine_tokens(double) == [second.password]


@pytest.mark.asyncio
async def test_issuing_again_replaces_the_machine_token_branvil_still_has(
    double: BranvilDouble,
):
    first = await owner_git_credential(BASE)
    assert first is not None
    # The keychain lost the token, which Branvil still takes.
    keyring.delete_password("GuildBotics/branvil-git", BASE)

    second = await owner_git_credential(BASE)

    assert second is not None
    assert _machine_tokens(double) == [second.password]


@pytest.mark.asyncio
async def test_issuing_again_revokes_the_token_of_the_machine_s_old_name(
    double: BranvilDouble,
):
    first = await owner_git_credential(BASE)
    assert first is not None
    set_device_display_name("renamed")
    keyring.delete_password("GuildBotics/branvil-git", BASE)

    second = await owner_git_credential(BASE)

    assert second is not None
    assert _machine_tokens(double) == [second.password]
    assert double.tokens[second.password]["label"].startswith("git:renamed (")


@pytest.mark.asyncio
async def test_a_token_another_git_issued_while_this_one_waited_is_handed(
    double: BranvilDouble, monkeypatch: pytest.MonkeyPatch
):
    """Two gits that find no token both wait for the lock; the second hands
    the token the first issued instead of revoking it with another."""
    issued = double.issue(OWNER, "git:elsewhere (00000000)", "bvt_first")

    @contextmanager
    def waited(*args: object, **kwargs: object) -> Iterator[None]:
        keyring.set_password("GuildBotics/branvil-git", BASE, "bvt_first")
        yield

    monkeypatch.setattr(owner_git, "held_lock", waited)

    credential = await owner_git_credential(BASE)

    assert credential is not None
    assert credential.password == "bvt_first"
    assert double.writes == []
    assert double.tokens["bvt_first"] == issued


@pytest.mark.asyncio
async def test_a_token_of_another_member_is_not_handed(double: BranvilDouble):
    keyring.set_password("GuildBotics/branvil-git", BASE, TOKEN)

    credential = await owner_git_credential(BASE)

    assert credential is not None
    assert credential.username == OWNER
    assert credential.password != TOKEN
    assert double.tokens[TOKEN]["member"] == MEMBER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:7879", "https://127.0.0.1:7878", "https://github.com"],
)
async def test_a_remote_of_another_host_is_left_to_other_helpers(
    double: BranvilDouble, origin: str
):
    assert await human_git_credential(origin) is None
    assert double.writes == []
    assert _held() is None


@pytest.mark.asyncio
async def test_a_machine_not_connected_to_branvil_hands_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    double = BranvilDouble()
    serve(double, monkeypatch)

    assert await human_git_credential(BASE) is None
    assert double.writes == []


@pytest.mark.asyncio
async def test_a_connection_without_its_admin_token_cannot_issue(
    double: BranvilDouble,
):
    identity_secret_store().delete("BRANVIL_ADMIN_TOKEN")

    with pytest.raises(ServiceError, match=t("integrations.branvil.api.not_connected")):
        await owner_git_credential(BASE)
    assert double.writes == []


@pytest.mark.asyncio
async def test_a_keychain_that_cannot_be_used_is_a_service_failure(
    double: BranvilDouble, monkeypatch: pytest.MonkeyPatch
):
    def unusable() -> None:
        raise SecretStoreError("locked")

    monkeypatch.setattr(owner_git, "system_keychain", unusable)

    with pytest.raises(ServiceError) as raised:
        await owner_git_credential(BASE)
    assert str(raised.value) == t(
        "integrations.branvil.owner_git.unavailable", reason="locked"
    )


@pytest.mark.parametrize(
    ("platform", "helper"),
    [
        ("win32", "!guildbotics git-credential"),
        # Quoted for the shell, then for the git configuration's value.
        ("darwin", "!'{home}/.guildbotics/bin/guildbotics' git-credential"),
    ],
)
def test_git_is_configured_to_ask_only_this_helper_for_branvil(
    fake_platform, monkeypatch: pytest.MonkeyPatch, platform: str, helper: str
):
    monkeypatch.setattr(managed_cli.Path, "home", lambda: managed_cli.Path("/my home"))
    fake_platform(managed_cli, platform)

    assert git_config(BASE) == (
        f'[credential "{BASE}"]\n'
        "\thelper =\n"
        f'\thelper = "{helper.format(home="/my home")}"\n'
    )
