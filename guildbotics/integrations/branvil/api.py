"""What Branvil's code host and its tickets share: the member's client of the
Branvil API, the addresses of tickets and pull requests, and the comments and
reactions on them.

Branvil is reached at one base URL, the same on every machine (Branvil's own
tunnel forwards it to the Hub elsewhere), so the URLs it is known by are the
ones recorded: a ticket is ``{base}/{tracker}/-/tickets/{key}``, a pull request
``{base}/{repository}/-/pulls/{number}``. Its API names a repository, a
tracker, or a board as one percent-encoded path segment.

Every request of the client that is not a read passes the write gate before
it is sent: it must name a repository or a tracker in the configured scope.
Every failure comes out as the ports' failure, told from Branvil's status and
error code.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import httpx

from guildbotics.entities import Person, Service, Team
from guildbotics.entities.message import Message
from guildbotics.integrations.repository_scope import check_path, refuse, valid_path
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.port import (
    SEMANTIC_REACTIONS,
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
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
from guildbotics.utils.i18n_tool import t

DEFAULT_BASE_URL = "http://127.0.0.1:7878"
PROVIDER = "branvil"
_TIMEOUT_SECONDS = 10.0
#: How much of Branvil's own words a failure repeats.
_MESSAGE_LIMIT = 500
_COLLECTIONS: dict[WorkKind, str] = {"ticket": "tickets", "pull_request": "pulls"}
_ITEM_NAMES: dict[WorkKind, re.Pattern[str]] = {
    "ticket": re.compile(r"[A-Z][A-Z0-9]{0,15}-[1-9][0-9]{0,9}"),
    "pull_request": re.compile(r"[1-9][0-9]{0,9}"),
}
#: The API path of a write: under the repository or the tracker it changes.
_WRITTEN = re.compile(r"/api/v1/(?:repos|trackers)/([^/]+)(?:/.*)?")


def base_url(config: Mapping[str, Any]) -> str:
    """Where a service's configuration says Branvil is reached."""
    return str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def username(person: Person) -> str:
    """The member's name on Branvil."""
    return str(person.account_info.get("branvil_username") or "")


def segment(path: str) -> str:
    """A repository, tracker, board, branch, or id as one API path segment."""
    return quote(path, safe="")


@dataclass(frozen=True)
class BranvilItem:
    """A ticket of a tracker, or a pull request of a repository."""

    kind: WorkKind
    #: The tracker or the repository.
    place: str
    #: The ticket's key or the pull request's number.
    name: str

    @property
    def api(self) -> str:
        collection = "trackers" if self.kind == "ticket" else "repos"
        return (
            f"/{collection}/{segment(self.place)}/{_COLLECTIONS[self.kind]}/{self.name}"
        )

    @property
    def label(self) -> str:
        return self.name if self.kind == "ticket" else f"!{self.name}"


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


def _failure(response: httpx.Response) -> ServiceError:
    """What Branvil's error answer means, in words fit for the user."""
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])[:_MESSAGE_LIMIT]
    except (ValueError, KeyError, TypeError):
        code, message = "", ""
    status = response.status_code
    if status == httpx.codes.UNAUTHORIZED:
        return ServiceError(t("integrations.branvil.api.authentication"))
    if status == httpx.codes.NOT_FOUND:
        return ServiceNotFound(t("integrations.branvil.api.not_found", message=message))
    if status == httpx.codes.UNPROCESSABLE_ENTITY:
        return ServiceUnsupported(
            t("integrations.branvil.api.unsupported", message=message)
        )
    if code in {"head_moved", "label_undefined"}:
        return ServiceError(t(f"integrations.branvil.api.{code}", message=message))
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


class _Client(httpx.AsyncClient):
    """A client whose failures to reach Branvil are the ports' failure."""

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except httpx.TimeoutException:
            raise Unreachable(
                t("integrations.branvil.api.timeout", base_url=self._origin)
            ) from None
        except httpx.RequestError:
            raise Unreachable(
                t("integrations.branvil.api.unreachable", base_url=self._origin)
            ) from None

    @property
    def _origin(self) -> str:
        return str(self.base_url).removesuffix("/api/v1/")


def connect(
    base_url: str, *, token: str, scope: str, person_id: str
) -> httpx.AsyncClient:
    """A client of the Branvil API at ``base_url``.

    Args:
        base_url: Where Branvil is reached.
        token: The member's token; empty for what needs none.
        scope: The namespace the client writes in.
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

    async def check(response: httpx.Response) -> None:
        if response.is_error:
            await response.aread()
            if response.status_code == httpx.codes.UNAUTHORIZED:
                record_auth_failure(person_id)
            raise _failure(response)

    return _Client(
        base_url=f"{base_url}/api/v1",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=_TIMEOUT_SECONDS,
        event_hooks={"request": [gate], "response": [check]},
    )


async def health(base_url: str) -> None:
    """Confirm that Branvil's API v1 answers at ``base_url``.

    Raises:
        ServiceError: If it does not.
    """
    async with connect(base_url, token="", scope="", person_id="") as client:
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


class BranvilApi:
    """A member's Branvil, as configured for one kind of service."""

    def __init__(self, person: Person, team: Team, service: Service) -> None:
        config = team.project.get_service_config(service)
        self.person = person
        self.team = team
        self.base_url = base_url(config)
        #: The namespace the member writes in.
        self.owner = str(config.get("owner") or "")
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
                scope=self.owner,
                person_id=self.person.person_id,
            )
        return self._client

    async def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """What Branvil answers to a write, which is sent once."""
        return _json(await self.client().request(method, path, json=body))

    async def get(self, path: str, **params: Any) -> Any:
        return _json(await self.client().get(path, params=params))

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
            or not _ITEM_NAMES[kind].fullmatch(name)
        ):
            raise ServiceNotFound(t(f"integrations.branvil.api.not_{kind}", url=url))
        return BranvilItem(kind, place, name)

    def url(self, item: BranvilItem) -> str:
        return f"{self.base_url}/{item.place}/-/{_COLLECTIONS[item.kind]}/{item.name}"

    def repository(self, path: str) -> str:
        """``path`` as a repository or a tracker of Branvil.

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
