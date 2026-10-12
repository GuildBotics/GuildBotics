"""Where a member writes.

A credential limited to selected repositories does not draw this line on
every service: GitHub, for one, accepts issues, comments, reviews, and
reactions on any public repository from them. So GuildBotics draws it
itself. Each service's provider configuration names a scope -- the namespace
the member writes in, an owner on GitHub -- and a member writes only to what
lies directly in it: a repository, or a tracker, whose path's parent is the
scope. Every provider passes each write through :func:`check_path` before it
is made, and the code host does so for every push of a member's git.
"""

from __future__ import annotations

import re

from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.port import ServiceError

#: One name of a path, as a URL of a service carries it.
NAME = re.compile(r"[A-Za-z0-9_.-]+")


def valid_path(path: str) -> bool:
    """Whether ``path`` is names joined by ``/``, a namespace and a name at
    least, none of them ``.`` or ``..``."""
    names = path.split("/")
    return len(names) > 1 and all(
        NAME.fullmatch(name) and name not in {".", ".."} for name in names
    )


def in_scope(scope: str, path: str) -> bool:
    """Whether ``path`` lies directly in the namespace ``scope``."""
    return (
        bool(scope)
        and valid_path(path)
        and path.rpartition("/")[0].casefold() == scope.casefold()
    )


def check_path(scope: str, path: str, *, provider: str) -> None:
    """Refuse a write to ``path`` unless it lies directly in ``scope``.

    Raises:
        ServiceError: If it does not.
    """
    if not in_scope(scope, path):
        refuse(scope, path, provider=provider)


def refuse(scope: str, target: str, *, provider: str) -> None:
    """Record the refused write ``target`` and refuse it.

    Raises:
        ServiceError: Always.
    """
    record_correlated_event(
        event_type="target.scope_refused",
        default_source=provider,
        attributes={"target.provider": provider, "target.scope": scope},
        payload={"target": target, "scope": scope},
    )
    raise ServiceError(
        f"Writes are limited to '{scope}', the scope the project is configured"
        f" with: refused {target}."
        if scope
        else f"No scope is configured for the project: refused {target}."
    )
