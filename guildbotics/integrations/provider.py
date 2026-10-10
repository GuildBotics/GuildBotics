"""What a provider is to the rest of the host: which kinds of service it
serves, what a member needs to use it, and how its setup is checked.

``integrations/factory.py`` holds one descriptor per provider and picks the
one ``project.services`` names for each kind; nothing else names a provider.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import Logger
from typing import Any, Literal

from pydantic import BaseModel, Field

from guildbotics.entities import Person, Team
from guildbotics.integrations.event_listener import EventListener
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.context import Context
from guildbotics.runtime.ticket_manager import TicketManager


class ProviderCheck(BaseModel):
    """One finding of a provider's verify or diagnose, as the Desktop shows it."""

    section: str
    code: str
    status: Literal["ok", "warning", "error"]
    message: str
    target: str = ""
    person_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


async def _no_diagnosis(context: Context, members: list[Person]) -> list[ProviderCheck]:
    return []


@dataclass(frozen=True)
class Chat:
    """What a provider's chat is made of."""

    service: Callable[[Logger, Person, Team], ChatService]
    #: The listener of one connection, for its members, which calls
    #: ``on_activity`` when it connects, disconnects, or receives.
    event_listener: Callable[
        [Logger, Team, list[Person], Callable[[], None]], EventListener
    ]
    #: The connection a member's chat events arrive on: the members of one key
    #: share one listener. Raises ``ChatCredentialsError`` for a member who
    #: cannot connect.
    listener_key: Callable[[Person, Team], str] = lambda person, team: ""


@dataclass(frozen=True)
class Provider:
    name: str
    #: The secrets a member may need, by their member-relative names: all the
    #: provider's package reads of a member's secrets.
    secret_keys: frozenset[str] = frozenset()
    #: All the ``account_info`` keys the provider's package reads.
    account_info_keys: frozenset[str] = frozenset()
    code_hosting: Callable[[Logger, Person, Team], CodeHostingService] | None = None
    ticket_manager: Callable[[Logger, Person, Team], TicketManager] | None = None
    chat: Chat | None = None
    #: Whether a member holds what the provider authenticates with.
    credentialed: Callable[[Person], bool] = lambda person: True
    #: Whether a member's configuration has what the provider needs, without
    #: reaching it.
    verify: Callable[[Person], list[ProviderCheck]] = lambda person: []
    #: Read-only checks against the provider itself, for these members (a
    #: human member is checked as someone tickets are assigned to).
    diagnose: Callable[[Context, list[Person]], Awaitable[list[ProviderCheck]]] = (
        _no_diagnosis
    )
