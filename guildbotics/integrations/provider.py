"""What a provider is to the rest of the host: which kinds of service it
serves, what a member needs to use it, and how its setup is checked.

``integrations/factory.py`` holds one descriptor per provider and picks the
one ``project.services`` names for each kind; nothing else names a provider.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from html import escape
from logging import Logger
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.event_listener import EventListener
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.context import Context
from guildbotics.runtime.ticket_service import TicketService
from guildbotics.utils.i18n_tool import t


class ProviderCheck(BaseModel):
    """One finding of a provider's verify or diagnose, as the Desktop shows it."""

    section: str
    code: str
    status: Literal["ok", "warning", "error"]
    message: str
    target: str = ""
    person_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    #: Whether the check is of a member's credential: its failing opens that
    #: member's credential alert for the section's provider.
    credential: bool = False


def configured_check(
    section: str, code: str, person: Person, key: str, target: str, configured: bool
) -> ProviderCheck:
    """Whether ``person`` has ``key`` configured, at ``target``, as verify says it."""
    return ProviderCheck(
        section=section,
        code=code,
        status="ok" if configured else "error",
        message=f"{target} is configured."
        if configured
        else f"{target} is not configured.",
        target=target,
        context={"person_id": person.person_id, "key": key},
    )


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
    #: share one listener. Raises ``ServiceError`` for a member who cannot
    #: connect.
    listener_key: Callable[[Person, Team], str] = lambda person, team: ""


class SetupError(ValueError):
    """What a setup refuses, as the screen reports it: a code it can name, and
    a sentence for the user -- given, or named by a locale ``key`` so that it
    is rendered in the language the screen asks for."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        key: str = "",
        status_code: int = 400,
    ) -> None:
        self.code = code
        self.key = key
        self.status_code = status_code
        self.message = message or self.localized("en")
        super().__init__(self.message)

    def localized(self, language: str) -> str:
        """This refusal's sentence in ``language``."""
        return t(self.key, locale=language) if self.key else self.message


class SetupItems(BaseModel):
    """What a setup stores, as a form gives it: no field the setup does not
    know, and no value with the whitespace around it, which the form's
    checks look past and the stored value would keep (an owner of
    ``" acme "`` scopes no repository)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AppCredential(BaseModel):
    """An app installed for a member on a host: what signs in as it."""

    app_id: str
    installation_id: str
    private_key: str


class HostCredential(BaseModel):
    """A member's credential on a host, as the provider of that host hands it
    to the member's other accounts: a token, or an app."""

    token: str = ""
    app: AppCredential | None = None


@dataclass(frozen=True)
class SavedMember:
    """A member as its accounts see it while it is saved or deleted."""

    #: The member's ID, as it is saved.
    person_id: str
    name: str
    human: bool
    #: The ``account_info`` entries of all its accounts.
    account_info: Mapping[str, str]
    #: A secret's value by its member-relative name, as the save leaves it
    #: ("" for none).
    get_secret: Callable[[str], str]
    #: ``project.services``: each kind's provider name and items.
    services: Mapping[str, Mapping[str, Any]]
    #: The member's credentials on other hosts, by the name of the provider
    #: that hands each over.
    credentials: Mapping[str, HostCredential] = field(default_factory=dict)
    #: The member's picture, as GuildBotics keeps it; ``None`` for none.
    avatar: Path | None = None


class MemberAccount(SetupItems):
    """A member's account with a provider, as the member screen edits it.

    A provider's account is the fields of its form; it is stored as
    ``account_info`` entries and the member's secrets, and read back as
    :meth:`view`. What a save needs of the provider itself is done by
    :meth:`prepare` before the save takes the workspace's lock, so that no
    write waits on a provider, and what the save makes safe to undo, by
    :meth:`saved` once it is done.
    """

    async def prepare(self, member: SavedMember) -> None:
        """Obtain from the provider what saving ``member`` needs, which
        :meth:`stored` then stores. Nothing the stored member relies on is
        undone: the save may yet be refused.

        Raises:
            SetupError: If the provider refuses it; nothing is saved.
        """

    @classmethod
    async def avatar_changed(cls, member: SavedMember) -> None:
        """Hand the provider ``member``'s picture, which has just changed, as
        :meth:`prepare` does on every save.

        Raises:
            SetupError: If the provider refuses it; the picture stays changed
                in GuildBotics.
        """

    @classmethod
    async def removed(cls, member: SavedMember) -> None:
        """Undo with the provider what saving ``member`` obtained, before the
        member is deleted.

        Raises:
            SetupError: If the provider refuses it; the member is kept.
        """

    @classmethod
    def credential(cls, member: SavedMember) -> HostCredential | None:
        """The member's credential on this provider's host, which its other
        accounts are handed when it is saved."""
        return None

    def stored(
        self, person_id: str, human: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        """The ``account_info`` entries and the secrets, by their
        member-relative names, a save of the member stores.

        Raises:
            SetupError: If what the form names cannot be stored.
        """
        raise NotImplementedError

    async def saved(self) -> None:
        """The member was saved with :meth:`stored`; it does not fail the
        save."""

    @classmethod
    def view(
        cls, account_info: Mapping[str, Any], has_secret: Callable[[str], bool]
    ) -> BaseModel:
        """The stored account as the member screen shows it: the values, and
        whether each secret is held."""
        raise NotImplementedError


@dataclass(frozen=True)
class Operation:
    """One request the setup screens make of a provider."""

    request: type[BaseModel]
    response: type[BaseModel]
    #: Answers ``request``; the team is the saved one, loaded when asked.
    run: Callable[[Any, Callable[[], Team]], Awaitable[BaseModel]]


@dataclass(frozen=True)
class Page:
    """What a browser opened on a provider's page is shown: an HTML page,
    or a redirect to ``redirect``."""

    html: str = ""
    redirect: str = ""
    status_code: int = 200


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>GuildBotics</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 4rem auto; max-width: 40rem;
       text-align: center; color: #333; }}
button {{ font-size: 1rem; padding: 0.5rem 1.5rem; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def html_page(body: str, status_code: int = 200) -> Page:
    """A page of GuildBotics with ``body``, which is HTML."""
    return Page(html=_PAGE.format(body=body), status_code=status_code)


def message_page(*paragraphs: str, status_code: int = 200) -> Page:
    """A page of GuildBotics saying ``paragraphs``, which are text."""
    return html_page(
        "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs),
        status_code,
    )


def page_path(provider: str, page: str) -> str:
    """Where a provider's page is served: a page is opened by a browser, which
    carries no session token."""
    return f"/providers/{provider}/pages/{page}"


@dataclass(frozen=True)
class ProviderSetup:
    """What the setup screens ask of a provider."""

    #: The provider's name as the screens show it.
    title: str
    #: The items of each kind of service it serves, as ``project.services``
    #: holds them besides the provider's name.
    services: Mapping[Service, type[BaseModel]] = field(default_factory=dict)
    member: type[MemberAccount] | None = None
    #: The URL of a member's avatar.
    avatar_url: Callable[[Person, Team], Awaitable[str]] | None = None
    #: The ``chat`` entries of a member's channel, from how the member screen
    #: names it.
    chat_channel: Callable[[str], dict[str, str]] | None = None
    #: The items every kind it serves must give alike.
    shared: frozenset[str] = frozenset()
    #: The providers whose member credentials its member accounts are handed
    #: (see :meth:`MemberAccount.credential`): a member's account with one is
    #: edited wherever this provider is used.
    credentials_from: frozenset[str] = frozenset()
    operations: Mapping[str, Operation] = field(default_factory=dict)
    #: Pages by name, from the query of the request that opened them.
    pages: Mapping[str, Callable[[Mapping[str, str]], Awaitable[Page]]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class Provider:
    """A provider, its services named as ``project.services`` names the kinds."""

    name: str
    #: The secrets a member may need, by their member-relative names: all the
    #: provider's package reads of a member's secrets.
    secret_keys: frozenset[str] = frozenset()
    #: All the ``account_info`` keys the provider's package reads.
    account_info_keys: frozenset[str] = frozenset()
    #: The workspace Secrets it keeps, by name: no member's, so its package
    #: reads them from the workspace's store, and the host alone uses them.
    workspace_secret_keys: frozenset[str] = frozenset()
    code_hosting_service: (
        Callable[[Logger, Person, Team], CodeHostingService] | None
    ) = None
    ticket_service: Callable[[Logger, Person, Team], TicketService] | None = None
    chat_service: Chat | None = None
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
    #: What the setup screens ask of it; a provider without one is not offered
    #: there.
    setup: ProviderSetup | None = None
    #: The port of the hub machine's loopback the project's services reach it
    #: at (given ``project.services``), which every other machine forwards to
    #: the hub; ``None`` when it is reached otherwise.
    hub_port: Callable[[Mapping[str, Mapping[str, Any]]], int | None] = (
        lambda services: None
    )
