"""Branvil as a provider: what verify asks of a member, what diagnose finds
on Branvil itself, and what the setup screens store."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from guildbotics.entities.team import Person
from guildbotics.integrations.branvil.setup import Workspace
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    SCOPE,
    BranvilDouble,
    services,
)

BRANVIL = PROVIDERS["branvil"]


class _Context:
    """What diagnose reads of a context: the team, and a member's services."""

    def __init__(self, team, code, tickets) -> None:
        self.team, self.code, self.tickets = team, code, tickets

    def clone_for(self, member: Person) -> _Context:
        return self

    def get_code_hosting_service(self):
        return self.code

    def get_ticket_service(self):
        return self.tickets

    async def aclose(self) -> None:
        await self.code.aclose()
        await self.tickets.aclose()


async def _diagnose(
    monkeypatch,
    double: BranvilDouble | None = None,
    code: dict[str, str] | None = None,
    **tickets,
):
    double, code, ticket_service = services(monkeypatch, double, code, **tickets)
    context = _Context(code.team, code, ticket_service)
    checks = await BRANVIL.diagnose(context, code.team.members)  # type: ignore[arg-type]
    return [(c.code, c.status, c.person_id, c.credential) for c in checks], checks


def test_an_agent_needs_its_name_and_token_and_a_human_its_name(monkeypatch):
    agent = Person(person_id="aiko", name="Aiko", person_type="agent")
    human = Person(
        person_id="otota",
        name="Otota",
        person_type="human",
        account_info={"branvil_username": "otota"},
    )
    monkeypatch.setenv("AIKO_BRANVIL_TOKEN", "bvt")

    assert [(c.target, c.status) for c in BRANVIL.verify(agent)] == [
        ("account_info.branvil_username", "error"),
        ("AIKO_BRANVIL_TOKEN", "ok"),
    ]
    assert [(c.target, c.status) for c in BRANVIL.verify(human)] == [
        ("account_info.branvil_username", "ok")
    ]
    assert BRANVIL.credentialed(agent) and not BRANVIL.credentialed(human)


@pytest.mark.asyncio
async def test_diagnose_reads_branvil_the_agents_token_and_the_workspace(monkeypatch):
    found, checks = await _diagnose(monkeypatch)

    assert found == [
        ("branvil_reachable", "ok", "", False),
        ("branvil_access", "ok", "aiko", True),
        ("branvil_workspace", "ok", "", False),
        ("human_branvil_user", "ok", "otota", False),
    ]
    assert checks[2].target == SCOPE


@pytest.mark.asyncio
async def test_each_workspace_the_services_name_is_read(monkeypatch):
    found, checks = await _diagnose(monkeypatch, code={"workspace": "nope"})

    assert found[2:4] == [
        ("branvil_workspace", "error", "", False),
        ("branvil_workspace", "ok", "", False),
    ]
    assert [check.target for check in checks[2:4]] == ["nope", SCOPE]


@pytest.mark.asyncio
async def test_a_branvil_out_of_reach_is_reported_once_and_alone(monkeypatch):
    double = BranvilDouble()

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    double.respond = unreachable  # type: ignore[method-assign]

    found, [check] = await _diagnose(monkeypatch, double)

    # An unreachable Branvil says nothing of anyone's token.
    assert found == [("branvil_reachable", "error", "", False)]
    assert check.message == t("integrations.branvil.api.unreachable", base_url=BASE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, text="Not Found"),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"service": "gitea"}),
    ],
    ids=["not_found", "not_json", "another_service"],
)
async def test_another_service_at_the_base_url_is_not_branvil(monkeypatch, answer):
    double = BranvilDouble()
    double.respond = lambda request: answer  # type: ignore[method-assign]

    found, [check] = await _diagnose(monkeypatch, double)

    assert found == [("branvil_reachable", "error", "", False)]
    assert check.message == t("integrations.branvil.api.not_branvil", base_url=BASE)


@pytest.mark.asyncio
async def test_a_refused_token_is_reported_and_no_workspace_read(monkeypatch):
    double = BranvilDouble()
    respond = double.respond
    double.respond = lambda request: (  # type: ignore[method-assign]
        httpx.Response(401, json={"error": {"code": "unauthorized", "message": ""}})
        if request.url.path.endswith("/me")
        else respond(request)
    )

    found, _ = await _diagnose(monkeypatch, double)

    assert found == [
        ("branvil_reachable", "ok", "", False),
        ("branvil_access", "error", "aiko", True),
        ("human_branvil_user", "ok", "otota", False),
    ]


@pytest.mark.parametrize(
    ("items", "stored"),
    [
        (
            {"workspace": "acme"},
            {"base_url": "http://127.0.0.1:7878", "workspace": "acme"},
        ),
        (
            {"workspace": "acme", "base_url": "http://hub.local:9000/"},
            {"base_url": "http://hub.local:9000", "workspace": "acme"},
        ),
    ],
)
def test_the_services_store_an_origin_and_a_workspace(
    items: dict[str, Any], stored: dict[str, Any]
):
    assert Workspace.model_validate(items).model_dump() == stored
    # The code host and the tickets name the same items.
    assert set(BRANVIL.setup.services.values()) == {Workspace}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "items",
    [
        {"workspace": ""},
        {"workspace": "acme/team"},
        {"workspace": ".."},
        {"workspace": "acme", "base_url": "ftp://hub"},
        {"workspace": "acme", "base_url": "http://hub/branvil"},
        {"workspace": "acme", "board": "acme/board"},
        {"owner": "acme"},
    ],
)
def test_the_services_refuse_what_names_no_branvil_or_workspace(
    items: dict[str, Any],
):
    with pytest.raises(ValidationError):
        Workspace.model_validate(items)


def test_a_member_account_stores_a_name_and_an_agents_token():
    account = BRANVIL.setup.member.model_validate(  # type: ignore[union-attr]
        {"username": "aiko", "token": "bvt"}
    )

    assert account.stored("aiko", human=False) == (
        {"branvil_username": "aiko"},
        {"BRANVIL_TOKEN": "bvt"},
    )
    assert account.stored("aiko", human=True) == ({"branvil_username": "aiko"}, {})
    view = BRANVIL.setup.member.view(  # type: ignore[union-attr]
        {"branvil_username": "aiko"}, lambda key: key == "BRANVIL_TOKEN"
    )
    assert view.model_dump() == {"username": "aiko", "has_token": True}
    with pytest.raises(ValidationError):
        BRANVIL.setup.member.model_validate({"username": "a b"})  # type: ignore[union-attr]
