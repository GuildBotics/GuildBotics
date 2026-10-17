"""Branvil as a provider: the code host and the tickets of a Branvil service
that every machine reaches at the same base URL."""

from __future__ import annotations

from typing import cast

from guildbotics.entities.team import Person, Service
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    Admin,
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
    """Whether Branvil answers, then whether a human is the owner whose
    rights GuildBotics' connection has, each agent's token, and the
    workspaces."""
    project = context.team.project
    kinds = [kind for kind in _KINDS if project.get_service_name(kind) == PROVIDER]
    if not kinds:
        return []
    # Both kinds reach one Branvil (setup holds them to one base URL).
    url = base_url(project.get_service_config(kinds[0]))
    checks = [await _reachable(url)]
    if checks[0].status == "error":
        # A token cannot be checked against a Branvil that does not answer.
        return checks
    #: Each workspace the kinds name, with a kind that names it.
    workspaces = {
        str(project.get_service_config(kind).get("workspace") or ""): kind
        for kind in kinds
    }
    humans = [member for member in members if member.person_type == "human"]
    owner = await _owner(url) if humans else ""
    for member in members:
        if member.person_type == "human":
            checks.append(_human(member, owner))
            continue
        access = await _access(context, member, kinds[0])
        checks.append(access)
        if access.status == "ok":
            for workspace, kind in workspaces.items():
                checks.append(await _workspace(context, member, kind, workspace))
            workspaces = {}
    return checks


def _service(context: Context, kind: Service) -> BranvilApi:
    return cast(
        BranvilApi,
        context.get_code_hosting_service()
        if kind == Service.CODE_HOSTING_SERVICE
        else context.get_ticket_service(),
    )


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


async def _owner(url: str) -> str | ServiceError:
    """Branvil's owner, as GuildBotics' connection names it, or why not."""
    try:
        async with Admin(url) as admin:
            return await admin.owner()
    except ServiceError as exc:
        return exc


def _human(member: Person, owner: str | ServiceError) -> ProviderCheck:
    """A human is Branvil's owner, whose rights GuildBotics' connection has:
    without the connection, nothing can be set up on Branvil."""
    name = username(member)
    if isinstance(owner, ServiceError):
        message = str(owner)
    elif name.casefold() != owner.casefold():
        message = t("integrations.branvil.provider.not_owner", name=name, owner=owner)
    else:
        message = ""
    return ProviderCheck(
        section=_SECTION,
        code="branvil_owner",
        credential=True,
        status="error" if message else "ok",
        message=message or t("integrations.branvil.provider.owner", owner=owner),
        person_id=member.person_id,
        target=name,
    )


async def _access(context: Context, member: Person, kind: Service) -> ProviderCheck:
    c = context.clone_for(member)
    try:
        await _service(c, kind).confirm()
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


async def _workspace(
    context: Context, member: Person, kind: Service, workspace: str
) -> ProviderCheck:
    c = context.clone_for(member)
    try:
        await _service(c, kind).check_workspace()
    except ServiceError as exc:
        return ProviderCheck(
            section=_SECTION,
            code="branvil_workspace",
            status="error",
            message=str(exc),
            target=workspace,
        )
    finally:
        await c.aclose()
    return ProviderCheck(
        section=_SECTION,
        code="branvil_workspace",
        status="ok",
        message=t("integrations.branvil.provider.workspace", workspace=workspace),
        target=workspace,
    )


BRANVIL = Provider(
    name=PROVIDER,
    secret_keys=frozenset({"BRANVIL_TOKEN"}),
    account_info_keys=frozenset({"branvil_username"}),
    workspace_secret_keys=frozenset({"BRANVIL_ADMIN_TOKEN"}),
    code_hosting_service=lambda logger, person, team: BranvilCodeHostingService(
        person, team
    ),
    ticket_service=BranvilTicketService,
    credentialed=lambda person: person.has_secret("BRANVIL_TOKEN"),
    verify=_verify,
    diagnose=_diagnose,
    setup=SETUP,
)
