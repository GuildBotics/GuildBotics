"""Every provider of the factory declares what it needs of a member.

A provider's descriptor declares which secrets and ``account_info`` keys of a
member it reads. What it declares is what its package reads, taken from the
code itself, so a key read but not declared (or declared but no longer read)
fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import guildbotics
from guildbotics.entities.team import Project, Team
from guildbotics.integrations.factory import PROVIDERS, configured_providers

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
    assert provider.code_hosting is not None or provider.ticket_manager is not None
    for kind in ("code_hosting_service", "ticket_manager"):
        team = Team(
            project=Project(services={kind: {"name": name.upper()}}), members=[]
        )
        assert configured_providers(team) == [provider]
    assert configured_providers(Team(project=Project(), members=[])) == []
