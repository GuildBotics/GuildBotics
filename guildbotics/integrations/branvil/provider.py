"""Branvil as a provider: the code host and the tickets of a Branvil service
that every machine reaches at the same base URL."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from guildbotics.entities.team import Person, Service
from guildbotics.integrations.branvil.api import (
    PROVIDER,
    Admin,
    BranvilApi,
    branvil_url,
    health,
    username,
)
from guildbotics.integrations.branvil.code_hosting import BranvilCodeHostingService
from guildbotics.integrations.branvil.owner_git import owner_git_credential
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
#: How often a load into the CI runner is asked after while it runs.
_POLL_SECONDS = 2.0


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
    # Every kind reaches the one Branvil the identities are connected to.
    url = branvil_url()
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


def _hub_port(services: Mapping[str, Mapping[str, Any]]) -> int | None:
    """The port of the Branvil the project reaches at a loopback base URL:
    Branvil runs once, on the hub machine, and every machine uses that same
    base URL."""
    if not any(item.get("name") == PROVIDER for item in services.values()):
        return None
    url = urlsplit(branvil_url())
    return url.port if url.hostname in ("127.0.0.1", "localhost") else None


async def _load_ci_image(archive: Path) -> list[str]:
    """Load ``archive``, a file on Branvil's machine, into its CI runner's
    image cache, and answer the references it loaded once the load is over.

    Branvil keeps only its latest image operation, without an identity, and
    answers even the request that began this load with whichever is the
    latest by then. A load of ``archive`` is therefore recognized by what it
    does: any load of the same file, whoever began it, loads what this one
    would.

    Raises:
        ServiceError: If Branvil refuses the load, the load fails, or its
            outcome is lost to a restart of Branvil or to another image
            operation.
    """

    def loads_archive(operation: dict[str, Any]) -> bool:
        return (operation["kind"], operation["target"]) == ("load", str(archive))

    async with Admin(branvil_url()) as admin:
        operation = (
            await admin.call("POST", "/admin/ci/images/load", {"path": str(archive)})
        )["operation"]
        while (
            operation and loads_archive(operation) and operation["state"] == "running"
        ):
            await asyncio.sleep(_POLL_SECONDS)
            operation = (await admin.get("/admin/ci"))["operation"]
    if not operation:
        raise ServiceError(
            t("integrations.branvil.provider.ci_load_lost", archive=archive)
        )
    if not loads_archive(operation):
        raise ServiceError(
            t("integrations.branvil.provider.ci_load_superseded", archive=archive)
        )
    if operation["state"] != "succeeded":
        raise ServiceError(
            t(
                "integrations.branvil.provider.ci_load_failed",
                archive=archive,
                detail=operation.get("detail") or "-",
            )
        )
    return [str(image["reference"]) for image in operation["loaded"]]


BRANVIL = Provider(
    name=PROVIDER,
    # The token's ID names the token a save replaces, for it to be revoked.
    secret_keys=frozenset({"BRANVIL_TOKEN", "BRANVIL_TOKEN_ID"}),
    account_info_keys=frozenset({"branvil_username"}),
    identity_secret_keys=frozenset({"BRANVIL_ADMIN_TOKEN"}),
    code_hosting_service=lambda logger, person, team: BranvilCodeHostingService(
        person, team
    ),
    ticket_service=BranvilTicketService,
    credentialed=lambda person: person.has_secret("BRANVIL_TOKEN"),
    verify=_verify,
    diagnose=_diagnose,
    setup=SETUP,
    hub_port=_hub_port,
    load_ci_image=_load_ci_image,
    human_git_credential=owner_git_credential,
)
