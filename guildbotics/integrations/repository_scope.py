"""Which repositories a member writes to.

A credential limited to selected repositories does not draw this line on
every code host: GitHub, for one, accepts issues, comments, reviews, and
reactions on any public repository from them. So GuildBotics draws it
itself. A member writes only to repositories of the owner the project is
configured with, and every provider passes each write through
:func:`check_repository` before it is made, as does every push of a member's
git.
"""

from __future__ import annotations

import re

from guildbotics.entities.team import Project, Service
from guildbotics.observability.diagnostics_events import record_correlated_event

#: An owner or repository name as a URL of a code host carries it.
NAME = re.compile(r"[A-Za-z0-9_.-]+")


class RepositoryScopeError(RuntimeError):
    """A write outside the repositories of the configured owner."""


def configured_owner(project: Project) -> str:
    """The owner whose repositories the project's members write to."""
    code = project.get_service_config(Service.CODE_HOSTING_SERVICE)
    ticket = project.get_service_config(Service.TICKET_MANAGER)
    return str(code.get("owner") or ticket.get("owner") or "")


def check_repository(scope: str, owner: str, repository: str) -> None:
    """Refuse a write to ``owner/repository`` unless ``scope`` owns it.

    Raises:
        RepositoryScopeError: If the repository is not the configured owner's.
    """
    if not in_scope(scope, owner, repository):
        refuse(scope, "/".join(name for name in (owner, repository) if name))


def in_scope(scope: str, owner: str, repository: str) -> bool:
    return (
        bool(scope)
        and all(
            NAME.fullmatch(name) and name not in {".", ".."}
            for name in (owner, repository)
        )
        and owner.casefold() == scope.casefold()
    )


def refuse(scope: str, target: str) -> None:
    """Record the refused write ``target`` and refuse it.

    Raises:
        RepositoryScopeError: Always.
    """
    record_correlated_event(
        event_type="github.scope_refused",
        default_source="github",
        attributes={"github.scope_owner": scope},
        payload={"target": target, "scope_owner": scope},
    )
    raise RepositoryScopeError(
        f"Writes are limited to repositories of '{scope}', "
        f"the owner the project is configured with: refused {target}."
        if scope
        else f"No owner is configured for the project: refused {target}."
    )
