"""Branvil as a provider: the code host and the tickets of a Branvil service
that every machine reaches at the same base URL."""

from __future__ import annotations

from typing import cast

from guildbotics.entities.team import Person, Service
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    BranvilApi,
    base_url,
    health,
    username,
)
from guildbotics.integrations.branvil.code_hosting import BranvilCodeHostingService
from guildbotics.integrations.branvil.setup import SETUP
from guildbotics.integrations.branvil.tickets import BranvilTicketService
from guildbotics.integrations.provider import (
    Provider,
    ProviderCheck,
    configured_check,
)
from guildbotics.runtime.context import Context
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t

_SECTION = PROVIDER
_KINDS = (Service.CODE_HOSTING_SERVICE, Service.TICKET_SERVICE)


def _verify(person: Person) -> list[ProviderCheck]:
    """Every member is known on Branvil by its name; an agent also holds a
    token."""
    checks = [
        configured_check(
            _SECTION,
            "branvil_credential",
            person,
            "branvil_username",
            "account_info.branvil_username",
            person.has_account_info("branvil_username"),
        )
    ]
    if person.person_type != "human":
        checks.append(
            configured_check(
                _SECTION,
                "branvil_credential",
                person,
                "BRANVIL_TOKEN",
                person.to_person_env_key("BRANVIL_TOKEN"),
                person.has_secret("BRANVIL_TOKEN"),
            )
        )
    return checks


async def _diagnose(context: Context, members: list[Person]) -> list[ProviderCheck]:
    """Whether Branvil answers, then each agent's token and the board."""
    project = context.team.project
    kinds = [kind for kind in _KINDS if project.get_service_name(kind) == PROVIDER]
    if not kinds:
        return []
    base_urls = sorted({base_url(project.get_service_config(kind)) for kind in kinds})
    checks = [await _reachable(base_url) for base_url in base_urls]
    if any(check.status == "error" for check in checks):
        # A token cannot be checked against a Branvil that does not answer.
        return checks
    tickets = Service.TICKET_SERVICE in kinds
    board = (
        str(project.get_service_config(Service.TICKET_SERVICE).get("board") or "")
        if tickets
        else ""
    )
    if tickets and not board:
        checks.append(
            ProviderCheck(
                section=_SECTION,
                code="branvil_board_missing",
                status="warning",
                message=t("integrations.branvil.provider.board_missing"),
            )
        )
    for member in members:
        if member.person_type == "human":
            checks.append(_human(member))
            continue
        access = await _access(context, member, kinds[0])
        checks.append(access)
        if board and access.status == "ok":
            checks.append(await _board(context, member, board))
            board = ""
    return checks


async def _reachable(url: str) -> ProviderCheck:
    try:
        await health(url)
    except ServiceError as exc:
        return ProviderCheck(
            section=_SECTION,
            code="branvil_reachable",
            status="error",
            message=str(exc),
            target=url,
        )
    return ProviderCheck(
        section=_SECTION,
        code="branvil_reachable",
        status="ok",
        message=t("integrations.branvil.provider.reachable", base_url=url),
        target=url,
    )


def _human(member: Person) -> ProviderCheck:
    configured = bool(username(member))
    return ProviderCheck(
        section=_SECTION,
        code="human_branvil_user",
        status="ok" if configured else "error",
        message=(
            t("integrations.branvil.provider.human_configured")
            if configured
            else t("integrations.branvil.provider.human_missing")
        ),
        person_id=member.person_id,
        target=username(member),
    )


async def _access(context: Context, member: Person, kind: Service) -> ProviderCheck:
    c = context.clone_for(member)
    try:
        service = (
            c.get_code_hosting_service()
            if kind == Service.CODE_HOSTING_SERVICE
            else c.get_ticket_service()
        )
        await cast(BranvilApi, service).confirm()
    except ServiceError as exc:
        return ProviderCheck(
            section=_SECTION,
            code="branvil_access",
            credential=True,
            status="error",
            message=str(exc),
            person_id=member.person_id,
        )
    finally:
        await c.aclose()
    return ProviderCheck(
        section=_SECTION,
        code="branvil_access",
        credential=True,
        status="ok",
        message=t("integrations.branvil.provider.access"),
        person_id=member.person_id,
    )


async def _board(context: Context, member: Person, board: str) -> ProviderCheck:
    c = context.clone_for(member)
    try:
        await cast(BranvilTicketService, c.get_ticket_service()).check_board()
    except ServiceError as exc:
        return ProviderCheck(
            section=_SECTION,
            code="branvil_board",
            status="error",
            message=str(exc),
            target=board,
        )
    finally:
        await c.aclose()
    return ProviderCheck(
        section=_SECTION,
        code="branvil_board",
        status="ok",
        message=t("integrations.branvil.provider.board", board=board),
        target=board,
    )


BRANVIL = Provider(
    name=PROVIDER,
    secret_keys=frozenset({"BRANVIL_TOKEN"}),
    account_info_keys=frozenset({"branvil_username"}),
    code_hosting_service=lambda logger, person, team: BranvilCodeHostingService(
        person, team
    ),
    ticket_service=BranvilTicketService,
    credentialed=lambda person: person.has_secret("BRANVIL_TOKEN"),
    verify=_verify,
    diagnose=_diagnose,
    setup=SETUP,
)
