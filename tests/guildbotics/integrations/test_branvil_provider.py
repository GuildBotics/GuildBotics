"""Branvil as a provider: what verify asks of a member, what diagnose finds
on Branvil itself, and what the setup screens store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from guildbotics.entities.team import Person
from guildbotics.integrations.branvil import provider as branvil_provider
from guildbotics.integrations.branvil.setup import Workspace
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    SCOPE,
    BranvilDouble,
    connect_admin,
    serve,
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
    connect_admin()

    found, checks = await _diagnose(monkeypatch)

    assert found == [
        ("branvil_reachable", "ok", "", False),
        ("branvil_access", "ok", "aiko", True),
        ("branvil_workspace", "ok", "", False),
        ("branvil_owner", "ok", "otota", True),
    ]
    assert checks[2].target == SCOPE
    assert checks[3].message == t("integrations.branvil.provider.owner", owner="otota")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admin", "message"),
    [
        ("", t("integrations.branvil.api.not_connected")),
        ("bvt_revoked", t("integrations.branvil.api.admin_refused")),
        # The agent's token is no owner's.
        ("bvt_aiko", t("integrations.branvil.api.admin_refused")),
    ],
    ids=["missing", "refused", "not_the_owners"],
)
async def test_a_human_is_the_owner_only_through_guildbotics_connection(
    monkeypatch, admin: str, message: str
):
    if admin:
        connect_admin(admin)

    found, checks = await _diagnose(monkeypatch)

    assert found[-1] == ("branvil_owner", "error", "otota", True)
    assert checks[-1].message == message


@pytest.mark.asyncio
async def test_a_human_named_otherwise_is_not_the_owner(monkeypatch):
    connect_admin()
    double = BranvilDouble()
    double.members["otota"]["name"] = "root"

    _, checks = await _diagnose(monkeypatch, double)

    assert (checks[-1].code, checks[-1].status) == ("branvil_owner", "error")
    assert checks[-1].message == t(
        "integrations.branvil.provider.not_owner", name="otota", owner="root"
    )


@pytest.mark.asyncio
async def test_each_workspace_the_services_name_is_read(monkeypatch):
    connect_admin()
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
    connect_admin()
    double = BranvilDouble()
    del double.tokens["bvt_aiko"]

    found, _ = await _diagnose(monkeypatch, double)

    assert found == [
        ("branvil_reachable", "ok", "", False),
        ("branvil_access", "error", "aiko", True),
        ("branvil_owner", "ok", "otota", True),
    ]


def test_the_services_store_a_workspace():
    """Where Branvil is, is the identities' connection's to say."""
    assert Workspace.model_validate({"workspace": "acme"}).model_dump() == {
        "workspace": "acme"
    }
    # The code host and the tickets name the same items.
    assert set(BRANVIL.setup.services.values()) == {Workspace}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "items",
    [
        {"workspace": ""},
        {"workspace": "acme/team"},
        {"workspace": ".."},
        {"workspace": "acme", "base_url": "http://hub:7878"},
        {"workspace": "acme", "board": "acme/board"},
        {"owner": "acme"},
    ],
)
def test_the_services_refuse_what_names_no_workspace(
    items: dict[str, Any],
):
    with pytest.raises(ValidationError):
        Workspace.model_validate(items)


_IMAGE = "guildbotics/agent-environment:dev"
_ARCHIVE = Path("/srv/images/agent-environment-arm64.tar")


async def _load_ci_image(
    monkeypatch, double: BranvilDouble, archive: Path = _ARCHIVE
) -> list[str]:
    serve(double, monkeypatch)
    monkeypatch.setattr(branvil_provider, "_POLL_SECONDS", 0)
    assert BRANVIL.load_ci_image is not None
    return await BRANVIL.load_ci_image(archive)


@pytest.mark.asyncio
async def test_an_archive_is_loaded_into_the_ci_runner_once_the_load_is_over(
    monkeypatch,
):
    connect_admin()
    double = BranvilDouble()
    double.ci_outcome, double.ci_reads = [_IMAGE], 3

    loaded = await _load_ci_image(monkeypatch, double)

    assert loaded == [_IMAGE]
    assert double.writes == ["POST /api/v1/admin/ci/images/load"]
    assert double.operation is not None
    assert (double.operation["target"], double.operation["state"]) == (
        str(_ARCHIVE),
        "succeeded",
    )
    assert double.ci_reads == 0


@pytest.mark.asyncio
async def test_a_failed_load_says_why(monkeypatch):
    connect_admin()
    double = BranvilDouble()
    double.ci_outcome = "the archive holds no image"

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double)

    assert str(raised.value) == t(
        "integrations.branvil.provider.ci_load_failed",
        archive=_ARCHIVE,
        detail="the archive holds no image",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archive", "running", "status", "code", "message"),
    [
        (_ARCHIVE, True, 409, "conflict", "Another image operation is running."),
        (
            Path("images/agent.tar"),
            False,
            400,
            "invalid",
            "Give the absolute path of the archive.",
        ),
    ],
    ids=["another_running", "relative"],
)
async def test_a_load_branvil_refuses_is_not_waited_for(
    monkeypatch, archive: Path, running: bool, status: int, code: str, message: str
):
    connect_admin()
    double = BranvilDouble()
    if running:
        double.operation = {"kind": "pull", "target": "x", "state": "running"}

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double, archive)

    assert str(raised.value) == t(
        "integrations.branvil.api.refused",
        operation="POST /api/v1/admin/ci/images/load",
        status=status,
        code=code,
        message=message,
    )
    assert double.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admin", "message"),
    [
        ("", t("integrations.branvil.api.not_connected")),
        # An agent's token administers nothing.
        ("bvt_aiko", "forbidden"),
    ],
    ids=["not_connected", "not_the_owners"],
)
async def test_only_guildbotics_connection_loads_into_the_ci_runner(
    monkeypatch, admin: str, message: str
):
    if admin:
        connect_admin(admin)
    double = BranvilDouble()

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double)

    assert message in str(raised.value)
    assert double.operation is None


def _replace_operation(
    monkeypatch,
    double: BranvilDouble,
    seen_in: str,
    kind: str,
    target: str,
    state: str = "succeeded",
) -> None:
    """Make Branvil's latest operation another one, in ``state``, by the time
    the answer named ``seen_in`` (``load``: the request that began the load;
    ``ci``: a later read) is built. A running one never ends."""
    answer = getattr(double, f"_{seen_in}")

    def replaced(*args: Any) -> dict[str, Any]:
        answer(*args)
        double.operation = {
            "kind": kind,
            "target": target,
            "state": state,
            "detail": "broken" if state == "failed" else None,
            "started_at": double.now(),
            "finished_at": None if state == "running" else double.now(),
            "loaded": [{"reference": "other/image:old"}]
            if state == "succeeded"
            else [],
        }
        return {"operation": dict(double.operation)}

    monkeypatch.setattr(double, f"_{seen_in}", replaced)


@pytest.mark.asyncio
@pytest.mark.parametrize("seen_in", ["load", "ci"])
@pytest.mark.parametrize("state", ["running", "succeeded"])
@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("pull", "other/image:old"),
        # A pull is told from a load of the archive even by the same name.
        ("pull", str(_ARCHIVE)),
        ("load", "/srv/images/other.tar"),
    ],
    ids=["a_pull", "a_pull_by_the_archives_name", "a_load_of_another_archive"],
)
async def test_a_load_whose_outcome_another_operation_replaced_is_not_reported(
    monkeypatch, seen_in: str, state: str, kind: str, target: str
):
    connect_admin()
    double = BranvilDouble()
    double.ci_outcome = [_IMAGE]
    _replace_operation(monkeypatch, double, seen_in, kind, target, state)

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double)

    assert str(raised.value) == t(
        "integrations.branvil.provider.ci_load_superseded", archive=_ARCHIVE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seen_in", ["load", "ci"])
async def test_any_load_of_the_same_archive_answers_for_this_one(
    monkeypatch, seen_in: str
):
    connect_admin()
    double = BranvilDouble()
    _replace_operation(monkeypatch, double, seen_in, "load", str(_ARCHIVE))

    assert await _load_ci_image(monkeypatch, double) == ["other/image:old"]


@pytest.mark.asyncio
async def test_a_failed_load_of_the_same_archive_fails_this_one(monkeypatch):
    connect_admin()
    double = BranvilDouble()
    _replace_operation(monkeypatch, double, "ci", "load", str(_ARCHIVE), "failed")

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double)

    assert str(raised.value) == t(
        "integrations.branvil.provider.ci_load_failed",
        archive=_ARCHIVE,
        detail="broken",
    )


@pytest.mark.asyncio
async def test_a_load_branvil_forgot_in_a_restart_is_reported_as_lost(monkeypatch):
    connect_admin()
    double = BranvilDouble()
    monkeypatch.setattr(double, "_ci", lambda: {"operation": None})

    with pytest.raises(ServiceError) as raised:
        await _load_ci_image(monkeypatch, double)

    assert str(raised.value) == t(
        "integrations.branvil.provider.ci_load_lost", archive=_ARCHIVE
    )
