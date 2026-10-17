"""Branvil's setup: GuildBotics' connection to Branvil, the items of its
services, and a member's Branvil account.

Connecting is a browser round trip that Branvil's consent screen ends: the
screen starts it here, the browser opens Branvil's consent page, and Branvil
sends the browser back to a page of this local API with a one-time code. The
code is exchanged here, with the PKCE verifier only this process holds, for
the admin token: an owner token Branvil labels with the client's name. It is
kept as the workspace Secret ``BRANVIL_ADMIN_TOKEN``, reaches the other
machines through the Hub's Secret transfer, and is used by the host alone: to
list and create workspaces and agents, to issue the agents' tokens, to hand
Branvil the members' GitHub credentials, and to open Branvil's GUI. Consents
in flight are held in memory only.

Every machine reaches Branvil at the same base URL (on a machine other than
the Hub, Branvil's tunnel forwards it), so the URL the project stores is the
one its records name, and one admin token serves both kinds of service.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from pydantic import AfterValidator, BaseModel, Field, PrivateAttr, field_validator

from guildbotics.entities.team import Service, Team
from guildbotics.integrations.branvil.api import (
    DEFAULT_BASE_URL,
    PROVIDER,
    Admin,
    Unauthorized,
    administer,
    base_url,
    health,
    segment,
)
from guildbotics.integrations.provider import (
    MemberAccount,
    Operation,
    Page,
    ProviderSetup,
    SavedMember,
    SetupError,
    SetupItems,
    message_page,
    page_path,
)
from guildbotics.integrations.repository_scope import valid_path
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.utils.env_loader import workspace_secret_store
from guildbotics.utils.i18n_tool import t

#: Where Branvil is reached: an origin, which its URLs start with.
_BASE_URL = re.compile(r"https?://[^/\s?#@]+")
#: A member's name on Branvil.
_MEMBER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: How GuildBotics names itself on Branvil's consent screen.
_CLIENT = "GuildBotics"
#: The label of the agents' tokens GuildBotics issues.
_TOKEN_LABEL = "guildbotics"
#: The hosts whose member credentials Branvil is handed: it publishes there
#: as the pull request's author.
_UPSTREAMS = frozenset({"github"})
_CONSENT_TTL_SECONDS = 10 * 60
_CONNECTED = "connected"


def _origin(value: str) -> str:
    value = value.rstrip("/") or DEFAULT_BASE_URL
    if not _BASE_URL.fullmatch(value):
        raise ValueError("base_url must be an http(s) origin: http://<host>:<port>")
    return value


def _workspace(value: str) -> str:
    # A workspace is one name, the first of its repositories' paths.
    if "/" in value or not valid_path(f"{value}/name"):
        raise ValueError("workspace must be one name, such as acme")
    return value


BaseUrl = Annotated[str, AfterValidator(_origin)]
WorkspaceName = Annotated[str, Field(min_length=1), AfterValidator(_workspace)]


class Workspace(SetupItems):
    """The Branvil the members reach, and the workspace they work in: they
    write only to its repositories and take tickets from its board."""

    base_url: BaseUrl = DEFAULT_BASE_URL
    workspace: WorkspaceName


def branvil_url(services: Mapping[str, Mapping[str, Any]]) -> str:
    """Where the project's services reach Branvil; "" when none is it."""
    return next(
        (base_url(item) for item in services.values() if item.get("name") == PROVIDER),
        "",
    )


@contextmanager
def _refusal() -> Iterator[None]:
    """Branvil's refusal, as the setup screens report it."""
    try:
        yield
    except ServiceError as exc:
        raise SetupError(PROVIDER, str(exc)) from exc


# -- the connection -----------------------------------------------------------


class Branvil(BaseModel):
    """The Branvil the form names, which may not be saved yet."""

    base_url: BaseUrl = DEFAULT_BASE_URL


class ConnectStart(Branvil):
    #: This local API as the user's browser reaches it; Branvil sends the
    #: browser back there.
    callback_base_url: str = Field(min_length=1)


class ConsentStarted(BaseModel):
    state: str
    #: Branvil's consent page, which the screen opens in the browser.
    url: str


class ConnectionQuery(Branvil):
    #: A consent in flight to follow.
    state: str = ""


class Connection(BaseModel):
    """GuildBotics' connection to Branvil, as the screen shows it."""

    status: Literal["connected", "pending", "disconnected"]
    #: The name of Branvil's owner, whose rights the connection has.
    owner: str = ""
    #: Why it is not connected.
    message: str = ""


@dataclass
class _Consent:
    base_url: str
    redirect_uri: str
    verifier: str
    created_at: float = field(default_factory=time.time)
    #: Empty while the owner decides; then ``_CONNECTED`` or why not.
    outcome: str = ""


class Consents:
    """The consents in flight, and the connection they make."""

    def __init__(self) -> None:
        self._held: dict[str, _Consent] = {}

    async def start(
        self, request: ConnectStart, team: Callable[[], Team]
    ) -> ConsentStarted:
        """The consent screen to open: only once Branvil answers there, as the
        browser would otherwise show nothing but a failed page."""
        with _refusal():
            await health(request.base_url)
        deadline = time.time() - _CONSENT_TTL_SECONDS
        self._held = {
            state: consent
            for state, consent in self._held.items()
            if consent.created_at >= deadline
        }
        state = secrets.token_urlsafe(32)
        consent = _Consent(
            base_url=request.base_url,
            redirect_uri=request.callback_base_url.rstrip("/")
            + page_path(PROVIDER, "connected"),
            verifier=secrets.token_urlsafe(48),
        )
        self._held[state] = consent
        challenge = urlsafe_b64encode(sha256(consent.verifier.encode()).digest())
        query = urlencode(
            {
                "client": _CLIENT,
                "redirect_uri": consent.redirect_uri,
                "state": state,
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
            }
        )
        return ConsentStarted(
            state=state, url=f"{request.base_url}/-/authorize?{query}"
        )

    async def connection(
        self, request: ConnectionQuery, team: Callable[[], Team]
    ) -> Connection:
        """The consent ``state`` names while the owner decides, and the
        connection this machine holds otherwise."""
        consent = self._held.get(request.state)
        if consent is not None:
            if not consent.outcome:
                return Connection(status="pending")
            del self._held[request.state]
            if consent.outcome != _CONNECTED:
                return Connection(status="disconnected", message=consent.outcome)
        try:
            async with Admin(request.base_url) as admin:
                return Connection(status="connected", owner=await admin.owner())
        except ServiceError as exc:
            return Connection(status="disconnected", message=str(exc))

    async def page(self, query: Mapping[str, str]) -> Page:
        """Where Branvil sends the browser back: exchange the code for the
        admin token, once."""
        consent = self._held.get(query.get("state", ""))
        if consent is None:
            return _failed_page(t("integrations.branvil.setup.expired"))
        if not consent.outcome:
            consent.outcome = await self._exchange(consent, query)
        if consent.outcome != _CONNECTED:
            return _failed_page(consent.outcome)
        return message_page(
            t("integrations.branvil.setup.connected"),
            t("integrations.branvil.setup.close"),
        )

    async def _exchange(self, consent: _Consent, query: Mapping[str, str]) -> str:
        code = query.get("code", "")
        if query.get("error") or not code:
            return t("integrations.branvil.setup.denied")
        try:
            async with administer(consent.base_url, "") as client:
                answer = await client.post(
                    "/apps/token",
                    json={
                        "code": code,
                        "redirect_uri": consent.redirect_uri,
                        "code_verifier": consent.verifier,
                    },
                )
        except ServiceError as exc:
            return str(exc)
        token = str(answer.json()["token"])
        await asyncio.to_thread(
            lambda: workspace_secret_store().set("BRANVIL_ADMIN_TOKEN", token)
        )
        return _CONNECTED


def _failed_page(reason: str) -> Page:
    return message_page(
        t("integrations.branvil.setup.failed"),
        reason,
        t("integrations.branvil.setup.retry"),
        status_code=400,
    )


# -- what the connection administers ------------------------------------------


class Workspaces(BaseModel):
    workspaces: list[str]


class NewWorkspace(Branvil):
    name: WorkspaceName


class Agents(BaseModel):
    """Branvil's agents, by name."""

    agents: list[str]


class Entry(BaseModel):
    #: A single-use link into Branvil's GUI, for the system browser.
    url: str


async def _workspaces(request: Branvil, team: Callable[[], Team]) -> Workspaces:
    with _refusal():
        async with Admin(request.base_url) as admin:
            found = await admin.items("/workspaces")
    return Workspaces(workspaces=[str(item["name"]) for item in found])


async def _create_workspace(
    request: NewWorkspace, team: Callable[[], Team]
) -> Workspaces:
    with _refusal():
        async with Admin(request.base_url) as admin:
            await admin.call("POST", "/workspaces", {"name": request.name})
    return await _workspaces(request, team)


async def _agents(request: Branvil, team: Callable[[], Team]) -> Agents:
    with _refusal():
        async with Admin(request.base_url) as admin:
            found = await admin.items("/members")
    return Agents(
        agents=[str(item["name"]) for item in found if item.get("kind") == "agent"]
    )


async def _entry(request: Branvil, team: Callable[[], Team]) -> Entry:
    with _refusal():
        async with Admin(request.base_url) as admin:
            link = await admin.call("POST", "/session/links")
    return Entry(url=str(link["url"]))


# -- a member's account -------------------------------------------------------


class AccountView(BaseModel):
    """A member's stored Branvil account, as the member screen shows it."""

    username: str = ""
    has_token: bool = False


class BranvilAccount(MemberAccount):
    """A member's Branvil account: the Branvil member it is. A human is
    Branvil's owner. An agent is the agent of its name, created unless it is
    there, and holds the token GuildBotics issues it."""

    #: The Branvil member, as chosen or saved; empty to name an agent after
    #: the member and a human after the owner.
    username: str = ""
    #: Issue an agent a new token though Branvil takes the one it holds.
    reissue: bool = False
    _token: str = PrivateAttr("")

    @field_validator("username")
    @classmethod
    def _name(cls, value: str) -> str:
        if value and not _MEMBER.fullmatch(value):
            raise ValueError("username must be a Branvil member name")
        return value

    async def prepare(self, member: SavedMember) -> None:
        """Name the member, and make an agent ready on Branvil: there, with
        a token Branvil takes for its own, and with its credentials on the
        hosts Branvil publishes to. Either way the Branvil member gets the
        member's picture."""
        url = branvil_url(member.services)
        if not url:
            return
        with _refusal():
            async with Admin(url) as admin:
                if member.human:
                    self.username = self.username or await admin.owner()
                    await _picture(admin, self.username, member)
                    return
                self.username = self.username or member.person_id
                await _agent(admin, self.username, member.name)
                if self.reissue or not await _accepted(
                    url, self.username, member.get_secret("BRANVIL_TOKEN")
                ):
                    issued = await admin.call(
                        "POST",
                        f"/members/{segment(self.username)}/tokens",
                        {"label": _TOKEN_LABEL, "replace": True},
                    )
                    self._token = str(issued["token"])
                for host in sorted(_UPSTREAMS & set(member.credentials)):
                    await admin.call(
                        "PUT",
                        f"/members/{segment(self.username)}/accounts/{host}",
                        member.credentials[host].model_dump(exclude_defaults=True),
                    )
                await _picture(admin, self.username, member)

    def stored(
        self, person_id: str, human: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        info = {"branvil_username": self.username} if self.username else {}
        return info, {"BRANVIL_TOKEN": self._token} if self._token else {}

    @classmethod
    async def avatar_changed(cls, member: SavedMember) -> None:
        """Give the Branvil member the member's new picture."""
        url = branvil_url(member.services)
        name = member.account_info.get("branvil_username", "")
        if not url or not name:
            return
        with _refusal():
            async with Admin(url) as admin:
                await _picture(admin, name, member)

    @classmethod
    async def removed(cls, member: SavedMember) -> None:
        """Revoke the tokens GuildBotics issued the agent; the agent stays,
        as the history names it."""
        url = branvil_url(member.services)
        name = member.account_info.get("branvil_username", "")
        if member.human or not url or not name:
            return
        tokens = f"/members/{segment(name)}/tokens"
        with _refusal():
            async with Admin(url) as admin:
                try:
                    issued = await admin.items(tokens)
                except ServiceNotFound:
                    return
                for token in issued:
                    if token.get("label") == _TOKEN_LABEL:
                        await admin.call(
                            "DELETE", f"{tokens}/{segment(str(token['id']))}"
                        )

    @classmethod
    def view(
        cls, account_info: Mapping[str, Any], has_secret: Callable[[str], bool]
    ) -> AccountView:
        return AccountView(
            username=str(account_info.get("branvil_username", "")),
            has_token=has_secret("BRANVIL_TOKEN"),
        )


async def _picture(admin: Admin, name: str, member: SavedMember) -> None:
    """Give the Branvil member ``name`` the member's picture, if it has one;
    the same picture again changes nothing there."""
    if member.avatar is not None:
        await admin.put_file(
            f"/members/{segment(name)}/avatar", member.avatar.read_bytes()
        )


async def _agent(admin: Admin, name: str, display_name: str) -> None:
    """Have Branvil's agent ``name``: the one there, or a new one.

    Raises:
        SetupError: If the member of that name is no agent.
    """
    try:
        found = await admin.get(f"/members/{segment(name)}")
    except ServiceNotFound:
        found = await admin.call(
            "POST",
            "/members",
            {"name": name, "display_name": display_name, "kind": "agent"},
        )
    if found.get("kind") != "agent":
        raise SetupError(PROVIDER, t("integrations.branvil.setup.not_agent", name=name))


async def _accepted(url: str, name: str, token: str) -> bool:
    """Whether Branvil takes ``token`` for the member ``name``'s."""
    if not token:
        return False
    async with administer(url, token) as client:
        try:
            me = (await client.get("/me")).json()
        except Unauthorized:
            return False
    return str(me.get("name") or "").casefold() == name.casefold()


consents = Consents()

SETUP = ProviderSetup(
    title="Branvil",
    services={
        Service.CODE_HOSTING_SERVICE: Workspace,
        Service.TICKET_SERVICE: Workspace,
    },
    member=BranvilAccount,
    # One Branvil per project: the admin token is one workspace Secret.
    shared=frozenset({"base_url"}),
    credentials_from=_UPSTREAMS,
    operations={
        "connect": Operation(ConnectStart, ConsentStarted, consents.start),
        "connection": Operation(ConnectionQuery, Connection, consents.connection),
        "workspaces": Operation(Branvil, Workspaces, _workspaces),
        "workspaces/create": Operation(NewWorkspace, Workspaces, _create_workspace),
        "agents": Operation(Branvil, Agents, _agents),
        "entry": Operation(Branvil, Entry, _entry),
    },
    pages={"connected": consents.page},
)
