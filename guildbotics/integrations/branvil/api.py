"""What Branvil's code host and its tickets share: the member's client of the
Branvil API, the addresses of tickets and pull requests, and the comments and
reactions on them.

Branvil is reached at one base URL, the same on every machine (Branvil's own
tunnel forwards it to the Hub elsewhere), which the identities' connection
records: one Branvil serves every workspace, whose members' identities and
admin token are the identities'. The URLs it is known by are the ones recorded: a ticket is ``{base}/{repository}/-/tickets/{number}``, a pull
request ``{base}/{repository}/-/pulls/{number}``; a repository
(``{workspace}/{name}``) numbers each from 1. Its API names a repository as
one percent-encoded path segment.

Every request of a member's client that is not a read passes the write gate
before it is sent: it must name a repository of the configured workspace, the
scope. GuildBotics' own client (:class:`Admin`), with the admin token Branvil
issued it on the owner's consent, administers Branvil for the setup screens,
the checks and the environment's images: members, their tokens and accounts,
workspaces, entry links, image loads into the CI runner.
It is the host's alone.
Every failure comes out as the ports' failure, told from Branvil's status and
error code.

Branvil restarts itself when its code moves on, and for those seconds it
refuses connections. A member's client sends a request again while it could
not connect at all, within the calling member command's remaining time. A
request that may have reached Branvil is never sent again, since a write
would land twice.
Once a wait ran out, no client in this process waits again for that endpoint
until any client receives an answer, so a new context does not renew the wait.
GuildBotics' own client does not wait: the setup screens and the check that
Branvil answers say whether it answers now.
Without a caller deadline, each request gets a finite operation budget:
one unavailable service must not occupy a host worker or interactive CLI
indefinitely. This bounds caller resource use, not predicted server downtime.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import httpx

from guildbotics.entities import Person, Service, Team
from guildbotics.entities.message import Message
from guildbotics.integrations.repository_scope import check_path, refuse, valid_path
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.member_invocation import current_member_invocation
from guildbotics.runtime.port import (
    SEMANTIC_REACTIONS,
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    ServiceUnavailable,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    CommentWritten,
    ReactionAdded,
    ReadModel,
    ReadPage,
    WorkKind,
    WorkTarget,
)
from guildbotics.utils.fileio import get_identity_config_dir, load_yaml_dict
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.secret_store import identity_secret_store

DEFAULT_BASE_URL = "http://127.0.0.1:7878"
PROVIDER = "branvil"
_TIMEOUT_SECONDS = 10.0
#: Limit an unbudgeted request's occupation of its caller to five minutes.
_UNBUDGETED_OPERATION_SECONDS = 300.0
_RECONNECT_LOCK = threading.Lock()


@dataclass
class _Reachability:
    """The endpoint's exhausted wait and responses observed by this process."""

    exhausted: bool = False
    answers: int = 0


_REACHABILITY: dict[str, _Reachability] = {}


def _reachability(origin: str) -> _Reachability:
    with _RECONNECT_LOCK:
        return _REACHABILITY.setdefault(origin, _Reachability())


#: How long it waits between two attempts to connect.
_RECONNECT_INTERVAL_SECONDS = 1.0
#: What fails before a request leaves for Branvil: no connection was made.
_NOT_CONNECTED = (httpx.ConnectError, httpx.ConnectTimeout)
#: How much of Branvil's own words a failure repeats.
_MESSAGE_LIMIT = 500
_COLLECTIONS: dict[WorkKind, str] = {"ticket": "tickets", "pull_request": "pulls"}
#: The number of a ticket or a pull request.
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
#: The API path of a write: under the repository it changes.
_WRITTEN = re.compile(r"/api/v1/repos/([^/]+)(?:/.*)?")


#: The identities' connection to Branvil, under their config directory:
#: where Branvil is reached, beside the admin token their store keeps.
CONNECTION_PATH = "connections/branvil.yml"


def connection_url() -> str:
    """Where the identities' connection reaches Branvil; "" before one is made."""
    connection = load_yaml_dict(get_identity_config_dir() / CONNECTION_PATH)
    return str(connection.get("base_url") or "").rstrip("/")


def branvil_url() -> str:
    """Where Branvil is reached: as connected, or at its default address."""
    return connection_url() or DEFAULT_BASE_URL


def username(person: Person) -> str:
    """The member's name on Branvil."""
    return str(person.account_info.get("branvil_username") or "")


def segment(path: str) -> str:
    """A repository, workspace, branch, or id as one API path segment."""
    return quote(path, safe="")


@dataclass(frozen=True)
class BranvilItem:
    """A ticket or a pull request of a repository."""

    kind: WorkKind
    #: The repository.
    place: str
    #: The number.
    name: str

    @property
    def api(self) -> str:
        return f"/repos/{segment(self.place)}/{_COLLECTIONS[self.kind]}/{self.name}"

    @property
    def label(self) -> str:
        return f"{'#' if self.kind == 'ticket' else '!'}{self.name}"


def record_auth_failure(person_id: str, code: str = "unauthorized") -> None:
    """Record that Branvil refused the member's token, or took it as another
    member's (``identity_mismatch``)."""
    record_correlated_event(
        event_type="credential.failed",
        default_source=PROVIDER,
        person_id=person_id,
        attributes={
            "credential.provider": PROVIDER,
            "error.category": "authentication",
        },
        payload={"provider": PROVIDER, "code": code},
    )


class Unauthorized(ServiceError):
    """Branvil took the token for nobody's."""


def _failure(response: httpx.Response) -> ServiceError:
    """What Branvil's error answer means, in words fit for the user."""
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])[:_MESSAGE_LIMIT]
    except (ValueError, KeyError, TypeError):
        code, message = "", ""
    status = response.status_code
    if status == httpx.codes.NOT_FOUND:
        return ServiceNotFound(t("integrations.branvil.api.not_found", message=message))
    if status == httpx.codes.UNPROCESSABLE_ENTITY:
        return ServiceUnsupported(
            t("integrations.branvil.api.unsupported", message=message)
        )
    if code == "head_moved":
        return ServiceError(t("integrations.branvil.api.head_moved"))
    if code == "label_undefined":
        return ServiceError(
            t("integrations.branvil.api.label_undefined", message=message)
        )
    if code == "invalid_cursor":
        return ServiceError(t("integrations.resources.continuation"))
    return ServiceError(
        t(
            "integrations.branvil.api.refused",
            operation=f"{response.request.method} {response.request.url.path}",
            status=status,
            code=code or "-",
            message=message,
        )
    )


class Unreachable(ServiceError):
    """Branvil did not answer at all."""


class NotConnected(Unreachable, ServiceUnavailable):
    """The request never left for Branvil."""


class _Client(httpx.AsyncClient):
    """A client whose failures to reach Branvil are the ports' failure, and
    which sends a request again while it cannot connect, within the caller's
    deadline, unless this endpoint's wait ran out with no answer since.

    Sending again is safe because the request never left: every body is
    bytes, which a second send repeats as they were.
    """

    def __init__(self, *, wait: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._wait = wait
        self._reachability = _reachability(self._origin)
        hooks = self.event_hooks
        self.event_hooks = {**hooks, "response": [self._answered, *hooks["response"]]}

    async def _answered(self, response: httpx.Response) -> None:
        with _RECONNECT_LOCK:
            self._reachability.exhausted = False
            self._reachability.answers += 1

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        # Resolve at send time: a client can outlive several member invocations.
        deadline = current_member_invocation().deadline if self._wait else None
        if self._wait and deadline is None:
            deadline = time.monotonic() + _UNBUDGETED_OPERATION_SECONDS
        while True:
            with _RECONNECT_LOCK:
                answers = self._reachability.answers
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError
                async with (
                    asyncio.timeout(max(0.0, deadline - time.monotonic()))
                    if deadline is not None
                    else nullcontext()
                ):
                    return await super().send(request, **kwargs)
            except (httpx.RequestError, TimeoutError) as exc:
                connected = not isinstance(exc, _NOT_CONNECTED)
                with _RECONNECT_LOCK:
                    exhausted = self._reachability.exhausted
                if not connected and deadline is not None and not exhausted:
                    left = deadline - time.monotonic()
                    if left > 0:
                        await asyncio.sleep(min(_RECONNECT_INTERVAL_SECONDS, left))
                        if time.monotonic() < deadline:
                            continue
                if self._wait and not connected:
                    with _RECONNECT_LOCK:
                        # A concurrent response invalidates this older refusal.
                        if self._reachability.answers == answers:
                            self._reachability.exhausted = True
                failure = Unreachable if connected else NotConnected
                raise failure(
                    t("integrations.branvil.api.timeout", base_url=self._origin)
                    if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                    else t(
                        "integrations.branvil.api.unreachable",
                        base_url=self._origin,
                    )
                ) from None

    @property
    def _origin(self) -> str:
        return str(self.base_url).removesuffix("/api/v1/")


def _client(
    base_url: str,
    token: str,
    wait: bool,
    unauthorized: Callable[[], ServiceError],
    *gate: Callable[[httpx.Request], Awaitable[None]],
) -> httpx.AsyncClient:
    """A client of the Branvil API at ``base_url`` with ``token``, which
    waits within the caller's deadline if ``wait`` is true, and whose
    refused token is ``unauthorized()``."""

    async def check(response: httpx.Response) -> None:
        if response.is_error:
            await response.aread()
            if response.status_code == httpx.codes.UNAUTHORIZED:
                raise unauthorized()
            raise _failure(response)

    return _Client(
        wait=wait,
        base_url=f"{base_url}/api/v1",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=_TIMEOUT_SECONDS,
        event_hooks={"request": list(gate), "response": [check]},
    )


def connect(
    base_url: str, *, token: str, scope: str, person_id: str
) -> httpx.AsyncClient:
    """A member's client of the Branvil API at ``base_url``, which waits
    for a Branvil that is restarting.

    Args:
        base_url: Where Branvil is reached.
        token: The member's token; empty for what needs none.
        scope: The workspace the client writes in.
        person_id: The member whose token a refusal is recorded for.
    """

    async def gate(request: httpx.Request) -> None:
        if request.method == "GET":
            return
        path = request.url.raw_path.decode("ascii").partition("?")[0]
        written = _WRITTEN.fullmatch(path)
        if written is None:
            refuse(scope, request.url.path, provider=PROVIDER)
        else:
            check_path(scope, unquote(written.group(1)), provider=PROVIDER)

    def unauthorized() -> ServiceError:
        record_auth_failure(person_id)
        return Unauthorized(t("integrations.branvil.api.authentication"))

    return _client(base_url, token, True, unauthorized, gate)


def administer(base_url: str, token: str) -> httpx.AsyncClient:
    """GuildBotics' own client of the Branvil API at ``base_url``, which
    writes to no repository and so passes no member's write gate, and which
    tells at once that Branvil does not answer.

    Args:
        base_url: Where Branvil is reached.
        token: The admin token, a member's token to check it, or empty for
            what needs none.
    """
    return _client(
        base_url,
        token,
        False,
        lambda: Unauthorized(t("integrations.branvil.api.admin_refused")),
    )


async def health(base_url: str) -> None:
    """Confirm that Branvil's API v1 answers at ``base_url`` now.

    Raises:
        ServiceError: If it does not.
    """
    async with administer(base_url, "") as client:
        try:
            answer = _json(await client.get("/health"))
        except Unreachable:
            raise
        except ServiceError:
            answer = None  # something that is not Branvil answered
    if not isinstance(answer, dict) or (answer.get("service"), answer.get("api")) != (
        PROVIDER,
        1,
    ):
        raise ServiceError(t("integrations.branvil.api.not_branvil", base_url=base_url))


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise ServiceError(t("integrations.branvil.api.invalid")) from None


class Calls:
    """The requests of one client of the Branvil API."""

    def client(self) -> httpx.AsyncClient:
        raise NotImplementedError

    async def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """What Branvil answers to a write, which is sent again only if it
        could not connect."""
        return _json(await self.client().request(method, path, json=body))

    async def get(self, path: str, **params: Any) -> Any:
        return _json(await self.client().get(path, params=params))

    async def put_file(self, path: str, content: bytes) -> Any:
        """What Branvil answers to a file put at ``path`` (a picture), sent
        as :meth:`call` sends a write."""
        return _json(await self.client().put(path, content=content))

    async def items(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every item of the collection at ``path``."""
        found: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page = await self.get(
                path, **params, limit=100, **({"cursor": cursor} if cursor else {})
            )
            found.extend(page["items"])
            cursor = page.get("next_cursor") or ""
            if not cursor:
                return found


class Admin(Calls):
    """Branvil as GuildBotics administers it with the admin token, which
    the identities' Secrets hold once the owner consented."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Admin:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    def client(self) -> httpx.AsyncClient:
        """The client with the admin token.

        Raises:
            ServiceError: If this machine holds none.
        """
        if self._client is None:
            token = identity_secret_store().get("BRANVIL_ADMIN_TOKEN")
            if not token:
                raise Unauthorized(t("integrations.branvil.api.not_connected"))
            self._client = administer(self.base_url, token)
        return self._client

    async def owner(self) -> str:
        """The name of Branvil's owner, whose rights the admin token has.

        Raises:
            ServiceError: If Branvil does not take it for the owner's.
        """
        me = await self.get("/me")
        if me.get("kind") != "owner":
            raise Unauthorized(t("integrations.branvil.api.admin_refused"))
        return str(me["name"])


class BranvilApi(Calls):
    """A member's Branvil, as configured for one kind of service."""

    def __init__(self, person: Person, team: Team, service: Service) -> None:
        config = team.project.get_service_config(service)
        self.person = person
        self.team = team
        self.base_url = branvil_url()
        #: The workspace the member writes in and takes tickets from: the scope.
        self.workspace = str(config.get("workspace") or "")
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def client(self) -> httpx.AsyncClient:
        """The member's client.

        Raises:
            ServiceError: If the member has no token.
        """
        if self._client is None:
            if not self.person.has_secret("BRANVIL_TOKEN"):
                raise ServiceError(
                    t(
                        "integrations.branvil.api.token_missing",
                        name=self.person.to_person_env_key("BRANVIL_TOKEN"),
                    )
                )
            self._client = connect(
                self.base_url,
                token=self.person.get_secret("BRANVIL_TOKEN"),
                scope=self.workspace,
                person_id=self.person.person_id,
            )
        return self._client

    async def page(
        self,
        resource: str,
        path: str,
        conditions: dict[str, Any],
        continuation: str,
        translate: Callable[[dict[str, Any]], ReadModel | dict[str, Any]],
    ) -> ReadPage:
        """The page of the collection at ``path`` that ``continuation`` (a
        cursor Branvil gave, bound to the collection) asks for."""
        page = await self.get(
            path,
            limit=conditions["page_size"],
            **({"cursor": continuation} if continuation else {}),
        )
        return ReadPage.of(
            resource,
            [translate(item) for item in page["items"]],
            continuation=page.get("next_cursor"),
        )

    async def confirm(self) -> None:
        """Confirm that Branvil accepts the member's token as the member's.

        Raises:
            ServiceError: If it does not.
        """
        if not username(self.person):
            raise ServiceError(t("integrations.branvil.api.username_missing"))
        me = await self.get("/me")
        name = str(me.get("name") or "")
        if name.casefold() != username(self.person).casefold():
            record_auth_failure(self.person.person_id, "identity_mismatch")
            raise ServiceError(
                t(
                    "integrations.branvil.api.identity_mismatch",
                    name=name,
                    username=username(self.person),
                )
            )

    async def check_workspace(self) -> None:
        """Confirm that the configured workspace is there for the member.

        Raises:
            ServiceError: If it is not.
        """
        await self.get(f"/workspaces/{segment(self.workspace)}")

    # -- addresses --------------------------------------------------------

    def serves(self, url: str) -> bool:
        """Whether ``url`` is an address of this Branvil."""
        return url.startswith(f"{self.base_url}/")

    def item(self, url: str, kind: WorkKind) -> BranvilItem:
        """The ticket or pull request ``url`` names.

        Raises:
            ServiceNotFound: If it names none of ``kind`` on this Branvil.
        """
        place, _, rest = (
            url.removeprefix(f"{self.base_url}/").rpartition("/-/")
            if self.serves(url)
            else ("", "", "")
        )
        collection, _, name = rest.partition("/")
        if (
            not valid_path(place)
            or collection != _COLLECTIONS[kind]
            or not _NUMBER.fullmatch(name)
        ):
            raise ServiceNotFound(
                t("integrations.branvil.api.not_ticket", url=url)
                if kind == "ticket"
                else t("integrations.branvil.api.not_pull_request", url=url)
            )
        return BranvilItem(kind, place, name)

    def url(self, item: BranvilItem) -> str:
        return f"{self.base_url}/{item.place}/-/{_COLLECTIONS[item.kind]}/{item.name}"

    def repository(self, path: str) -> str:
        """``path`` as a repository of Branvil.

        Raises:
            ServiceNotFound: If it is not one.
        """
        if not valid_path(path):
            raise ServiceNotFound(t("integrations.branvil.api.not_path", path=path))
        return path

    def target(self, item: BranvilItem, data: dict[str, Any]) -> WorkTarget:
        return WorkTarget(
            kind=item.kind,
            url=self.url(item),
            title=str(data.get("title") or ""),
            label=item.label,
        )

    # -- what members say -------------------------------------------------

    def author_type(self, author: object) -> str:
        if not author:
            return ""
        mine = str(author).casefold() == username(self.person).casefold()
        return Message.ASSISTANT if mine else Message.USER

    def message(self, comment: dict[str, Any]) -> Message:
        """A comment as a task carries it, by the team member who wrote it."""
        author = str(comment.get("author") or "")
        author_type = self.author_type(author)
        name = next(
            (
                member.name
                for member in self.team.members
                if author and username(member).casefold() == author.casefold()
            ),
            "",
        )
        return Message(
            content=str(comment.get("body") or ""),
            author=name or author_type,
            author_type=author_type,
            timestamp=str(comment.get("created_at") or ""),
        )

    def comment_of(self, comment: dict[str, Any]) -> dict[str, Any]:
        """A comment, as the ports read one."""
        return {
            "id": str(comment["id"]),
            "body": str(comment.get("body") or ""),
            "author": str(comment.get("author") or ""),
            "author_type": self.author_type(comment.get("author")),
            "created_at": comment.get("created_at"),
            "url": comment.get("url"),
        }

    def mentions(self, text: str, name: str | None = None) -> bool:
        """Whether ``text`` mentions ``name``, the member by default."""
        name = username(self.person) if name is None else name
        return bool(name) and (
            re.search(
                # A name may hold a dot, but a sentence's dot ends it.
                rf"(?<![\w.@-])@{re.escape(name)}(?![\w-]|\.[\w-])",
                text,
                flags=re.IGNORECASE | re.ASCII,
            )
            is not None
        )

    async def post_comment(
        self, item: BranvilItem, body: str, status: WorkflowStatus | None
    ) -> CommentWritten:
        """Comment on ``item``; a status is said to its author.

        The item is read before the write, never after it: a read that fails
        after the comment landed would make a retry post it twice.
        """
        data = await self.get(item.api)
        written = await self.call(
            "POST",
            f"{item.api}/comments",
            {"body": body.rstrip()}
            if status is None
            else {
                "body": self._addressed(body.rstrip(), str(data.get("author") or "")),
                "meta": status.payload(),
            },
        )
        return CommentWritten(
            id=str(written["id"]),
            url=written.get("url"),
            author=str(written.get("author") or ""),
            created_at=written.get("created_at"),
            target=self.target(item, data),
        )

    async def react(
        self, item: BranvilItem, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        """React to the comment or review of ``item`` named ``target_id``.

        Raises:
            ServiceUnsupported: If ``reaction`` is no meaning a reaction has.
        """
        if reaction not in SEMANTIC_REACTIONS:
            raise ServiceUnsupported(f"Unsupported semantic reaction: {reaction}")
        await self.call(
            "POST",
            f"{item.api}/reactions",
            {"target_id": target_id, "reaction": reaction},
        )
        return ReactionAdded(reaction=reaction, id=target_id)

    def _addressed(self, body: str, author: str) -> str:
        """``body`` with a mention of the item's author, unless the member is
        the author or the body already mentions them."""
        if self.author_type(author) != Message.USER or self.mentions(body, author):
            return body
        return f"@{author}\n\n{body}"
