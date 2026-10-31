"""Every provider of the factory declares what it needs of a member.

A provider's descriptor declares which secrets and ``account_info`` keys of a
member it reads, and which workspace Secrets it keeps. What it declares is
what its package reads, taken from the code itself, so a key read but not
declared (or declared but no longer read) fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import guildbotics
from guildbotics.entities.team import Project, Service, Team
from guildbotics.integrations.factory import (
    PROVIDERS,
    ci_image_loader,
    configured_providers,
)
from guildbotics.integrations.provider import SetupItems

PACKAGE = Path(guildbotics.__file__).parent / "integrations"
#: The calls that read a member's secret by name.
_SECRET_READS = {"has_secret", "get_secret", "to_person_env_key"}
#: The calls that read a member's ``account_info`` by key.
_ACCOUNT_READS = {"has_account_info", "get_account_info"}


def _read_keys(name: str) -> tuple[set[str], set[str]]:
    """The secret names and ``account_info`` keys the provider's package reads."""
    secrets: set[str] = set()
    accounts: set[str] = set()
    for path in sorted((PACKAGE / name).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "account_info"
                and isinstance(node.slice, ast.Constant)
            ):
                accounts.add(str(node.slice.value))
            if not isinstance(node, ast.Call) or not node.args:
                continue
            key = node.args[0]
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            func = node.func
            method = func.attr if isinstance(func, ast.Attribute) else ""
            if method in _SECRET_READS:
                secrets.add(key.value.upper())
            elif method in _ACCOUNT_READS or (
                method == "get"
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "account_info"
            ):
                accounts.add(key.value)
    return secrets, accounts


def _identity_secrets(name: str) -> set[str]:
    """The names the provider's package reads from or keeps in the
    identities' store without a member's prefix: its connection's."""
    found: set[str] = set()
    for path in sorted((PACKAGE / name).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "set"}
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "identity_secret_store"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(str(node.args[0].value))
    return found


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_a_provider_declares_exactly_the_connection_secrets_it_keeps(name):
    assert PROVIDERS[name].identity_secret_keys == _identity_secrets(name)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_a_provider_declares_exactly_what_its_package_reads_of_a_member(name):
    provider = PROVIDERS[name]
    secrets, accounts = _read_keys(name)

    assert provider.secret_keys == secrets
    assert provider.account_info_keys == accounts


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_a_provider_serves_a_kind_and_is_chosen_by_its_name(name):
    provider = PROVIDERS[name]
    assert provider.name == name
    kinds = {
        "code_hosting_service": provider.code_hosting_service,
        "ticket_service": provider.ticket_service,
        "chat_service": provider.chat_service,
    }
    assert any(kinds.values())
    for kind in (kind for kind, serves in kinds.items() if serves):
        team = Team(
            project=Project(services={kind: {"name": name.upper()}}), members=[]
        )
        assert configured_providers(team) == [provider]
    assert configured_providers(Team(project=Project(), members=[])) == []


#: A member account of each provider with every field it stores filled,
#: and the secrets the form gives (Branvil's token is issued by the save).
_FILLED_ACCOUNTS: dict[str, tuple[dict[str, Any], set[str]]] = {
    "branvil": ({"username": "aiko", "reissue": True}, set()),
    "github": (
        {
            "account_type": "github_apps",
            "username": "octo",
            "installation_id": 7,
            "app_id": 3,
            "access_token": "ghp-token",
            "private_key": "-----BEGIN KEY-----",
        },
        {"GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_KEY"},
    ),
    "slack": (
        {"user_id": "U1", "bot_token": "xoxb-1", "app_token": "xapp-1"},
        {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"},
    ),
}


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_a_provider_is_offered_by_setup_for_exactly_the_kinds_it_serves(name):
    provider = PROVIDERS[name]
    setup = provider.setup
    if name == "local":
        # The reference provider is not offered by the setup screens.
        assert setup is None
        return
    assert setup is not None
    served = {kind for kind in Service if getattr(provider, kind.value) is not None}
    assert set(setup.services) == served


@pytest.mark.parametrize("name", sorted(n for n, p in PROVIDERS.items() if p.setup))
def test_what_a_setup_stores_is_read_without_unknown_fields_or_whitespace(name):
    # A value the form's checks trim, stored as it was typed, means something
    # else to whatever reads it (an owner of " acme " scopes no repository).
    setup = PROVIDERS[name].setup
    assert setup is not None
    models = [*setup.services.values(), *([setup.member] if setup.member else [])]
    for model in models:
        assert issubclass(model, SetupItems), model


@pytest.mark.parametrize(
    "name", sorted(n for n, p in PROVIDERS.items() if p.setup and p.setup.member)
)
def test_a_member_account_stores_only_the_secrets_its_provider_declares(name):
    provider = PROVIDERS[name]
    assert provider.setup is not None and provider.setup.member is not None
    fields, given = _FILLED_ACCOUNTS[name]
    account = provider.setup.member.model_validate(fields)

    info, secrets = account.stored("alice", human=False)
    human_info, human_secrets = account.stored("alice", human=True)

    assert set(secrets) == given <= provider.secret_keys
    assert human_secrets == {}
    assert set(info) | set(human_info) <= provider.account_info_keys


#: The checks of a member's credential: their failing opens the member's
#: credential alert for the provider, their passing closes it.
_CREDENTIAL_CHECKS = {
    "branvil": {"branvil_access", "branvil_owner"},
    "github": {"github_access", "github_code_hosting_access", "github_project_access"},
    "slack": {
        "slack_access",
        "slack_app_token",
        "slack_app_token_invalid",
        "slack_bot_auth",
    },
}


def _checks(name: str) -> dict[str, set[str]]:
    """The codes of the checks the provider's package makes, by whether each
    is flagged as of a member's credential."""
    flagged: dict[str, set[str]] = {"credential": set(), "other": set()}
    for path in sorted((PACKAGE / name).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProviderCheck"
            ):
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            code = keywords.get("code")
            assert isinstance(code, ast.Constant), ast.unparse(node)
            credential = keywords.get("credential")
            is_credential = isinstance(credential, ast.Constant) and bool(
                credential.value
            )
            flagged["credential" if is_credential else "other"].add(code.value)
    return flagged


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_the_checks_of_a_members_credential_are_exactly_those_flagged(name):
    checks = _checks(name)

    assert checks["credential"] == _CREDENTIAL_CHECKS.get(name, set())
    # A code is either of a credential or not, wherever it is made.
    assert not checks["credential"] & checks["other"]


@pytest.mark.parametrize(
    ("code_host", "loads"),
    [("branvil", True), ("github", False), ("local", False), ("", False)],
)
def test_only_branvils_ci_runner_is_loaded_with_images(
    code_host: str, loads: bool
) -> None:
    """The project's code host chooses the CI runner an image is loaded into;
    only Branvil's takes one from GuildBotics."""
    services = {"code_hosting_service": {"name": code_host}} if code_host else {}
    team = Team(project=Project(services=services), members=[])

    assert ci_image_loader(team) is (
        PROVIDERS["branvil"].load_ci_image if loads else None
    )
    assert [n for n, p in PROVIDERS.items() if p.load_ci_image] == ["branvil"]
