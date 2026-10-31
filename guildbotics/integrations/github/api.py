"""What GitHub's code host and its tickets share: the member's client, the
addresses of issues and pull requests, and the comments and reactions on them.

An issue is a ticket, kept in its repository (its tracker) and numbered
together with the repository's pull requests. A comment's id carries its kind
the way GitHub's own anchors spell it (``issuecomment-<n>``,
``discussion_r<n>``, ``pullrequestreview-<n>``), so a reaction or a reply
reaches the collection the id belongs to.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from httpx import AsyncClient

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.github.github_utils import (
    create_github_client,
    get_author_type,
    paginated_items,
)
from guildbotics.integrations.github.identity import member_identity
from guildbotics.integrations.github.repository_scope import ADD_REACTION
from guildbotics.integrations.github.workflow_status_comment import (
    render_workflow_status_comment,
)
from guildbotics.integrations.repository_scope import valid_path
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    MAX_PAGE_BYTES,
    CommentWritten,
    ReactionAdded,
    WorkKind,
    WorkTarget,
)
from guildbotics.utils.i18n_tool import t

DEFAULT_API = "https://api.github.com"
#: How an item's web address names its kind.
_WEB_COLLECTIONS: dict[WorkKind, str] = {"ticket": "issues", "pull_request": "pull"}
#: An item's web address: owner, repository, collection, number.
_ITEM_PATH_PARTS = 4
#: Each kind of comment id, by the anchor GitHub gives it.
CONVERSATION = "issuecomment-"
INLINE = "discussion_r"
REVIEW = "pullrequestreview-"
_REACTIONS: dict[SemanticReaction, str] = {
    "ack": "THUMBS_UP",
    "agree": "THUMBS_UP",
    "celebrate": "HOORAY",
    "support": "HEART",
}


@dataclass(frozen=True)
class GitHubItem:
    """An issue or a pull request: GitHub numbers both in one sequence of
    their repository."""

    repository: str
    number: int
    kind: WorkKind

    @property
    def api(self) -> str:
        collection = "pulls" if self.kind == "pull_request" else "issues"
        return f"/repos/{self.repository}/{collection}/{self.number}"

    @property
    def conversation(self) -> str:
        """Where its conversation is: an issue's for either kind."""
        return f"/repos/{self.repository}/issues/{self.number}"

    @property
    def label(self) -> str:
        return f"#{self.number}"


def comment_id(prefix: str, value: object) -> str:
    """The id of a comment of a kind, from GitHub's number for it.

    Raises:
        ValueError: If GitHub gave no number.
    """
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid comment id")
    return f"{prefix}{value}"


def number_of(comment: str, prefix: str) -> int:
    """GitHub's number of a comment id of the kind ``prefix`` names.

    Raises:
        ServiceNotFound: If it is not an id of that kind.
    """
    number = comment.removeprefix(prefix)
    if number == comment or not re.fullmatch(r"[1-9][0-9]{0,19}", number):
        raise ServiceNotFound(f"'{comment}' names no comment of this kind.")
    return int(number)


class GitHubApi:
    """A member's GitHub, as configured for one kind of service."""

    def __init__(self, person: Person, team: Team, service: Service) -> None:
        config = team.project.get_service_config(service)
        self.person = person
        self.team = team
        self.base_url = str(config.get("api_base_url") or DEFAULT_API).rstrip("/")
        #: The owner whose repositories the member writes to.
        self.owner = str(config.get("owner") or "")
        self._client: AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def client(self) -> AsyncClient:
        if self._client is None:
            self._client = await create_github_client(
                self.person, self.base_url, self.owner
            )
        return self._client

    def web_base_url(self) -> str:
        if self.base_url in ("https://api.github.com", "http://api.github.com"):
            return "https://github.com"
        for suffix in ("/api/v3", "/api"):
            if self.base_url.endswith(suffix):
                return self.base_url.removesuffix(suffix)
        return self.base_url

    def serves(self, url: str) -> bool:
        """Whether ``url`` is an address of this GitHub."""
        parsed, web = urlparse(url), urlparse(self.web_base_url())
        return (parsed.scheme, parsed.netloc) == (web.scheme, web.netloc)

    def item(self, url: str, kind: WorkKind) -> GitHubItem:
        """The issue or pull request ``url`` names.

        Raises:
            ServiceNotFound: If it names none of ``kind`` on this GitHub.
        """
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if (
            not self.serves(url)
            or parsed.query
            or parsed.fragment
            or len(parts) != _ITEM_PATH_PARTS
            or parts[2] != _WEB_COLLECTIONS[kind]
            or not re.fullmatch(r"[1-9][0-9]{0,9}", parts[3])
            or not valid_path("/".join(parts[:2]))
        ):
            noun = "pull request" if kind == "pull_request" else "issue"
            raise ServiceNotFound(f"{url} is not a GitHub {noun} URL.")
        return GitHubItem("/".join(parts[:2]), int(parts[3]), kind)

    def url(self, item: GitHubItem) -> str:
        return (
            f"{self.web_base_url()}/{item.repository}/"
            f"{_WEB_COLLECTIONS[item.kind]}/{item.number}"
        )

    def repository(self, path: str) -> str:
        """``path`` as a GitHub repository: ``owner/repo``.

        Raises:
            ServiceNotFound: If it is not one.
        """
        if not valid_path(path) or path.count("/") != 1:
            raise ServiceNotFound(f"'{path}' is not a GitHub repository (owner/repo).")
        return path

    def target(self, item: GitHubItem, data: dict[str, Any]) -> WorkTarget:
        """``item`` as a work target, at the address GitHub spells it."""
        return WorkTarget(
            kind=item.kind,
            url=str(data.get("html_url") or self.url(item)),
            title=str(data.get("title") or ""),
            label=item.label,
        )

    def author_type(self, data: dict[str, Any]) -> str:
        login = str((data.get("user") or {}).get("login") or "")
        return get_author_type(self.person, login) if login else ""

    async def get(self, path: str, **kwargs: Any) -> Any:
        """What GitHub answers for ``path``, as JSON."""
        response = await (await self.client()).get(path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise ServiceError(t("integrations.github.response.response")) from None

    async def paginated(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Every item of the REST collection at ``path``."""
        client = await self.client()

        def parse_page(response: Any) -> list[dict[str, Any]]:
            page = response.json()
            if not isinstance(page, list):
                raise ServiceError(t("integrations.github.response.response"))
            return [item for item in page if isinstance(item, dict)]

        async for item in paginated_items(
            client.get, path, parse_page, params=params, headers=headers
        ):
            yield item

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await (await self.client()).post(
            "/graphql", json={"query": query, "variables": variables}
        )
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors or not isinstance(payload, dict):
            raise graphql_error(errors)
        return payload["data"]

    async def fetch(
        self,
        item: GitHubItem,
        *,
        bounded: bool = False,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """The issue or pull request ``item`` names.

        Args:
            item: What to fetch.
            bounded: Hold the response to one read page, for a read that
                returns the item; what reads only some of its fields is not.
            headers: A media type needed by a provider's read.

        Raises:
            ServiceNotFound: If there is none of its kind: GitHub answers a
                pull request at an issue's address too.
        """
        data = await self.get(
            item.api,
            headers=headers,
            follow_redirects=False,
            extensions={"max_response_bytes": MAX_PAGE_BYTES} if bounded else {},
        )
        if not isinstance(data, dict):
            raise ServiceError(t("integrations.github.response.response"))
        if item.kind == "ticket" and "pull_request" in data:
            raise ServiceNotFound(
                f"{item.repository}#{item.number} is a pull request, not an issue."
            )
        return data

    def conversation_comment(self, data: dict[str, Any]) -> dict[str, Any]:
        """A conversation comment, as the ports read one."""
        return {
            "id": comment_id(CONVERSATION, data.get("id")),
            "body": str(data.get("body") or ""),
            "author": str((data.get("user") or {}).get("login") or ""),
            "author_type": self.author_type(data),
            "created_at": data.get("created_at"),
            "url": data.get("html_url"),
        }

    async def post_comment(
        self, item: GitHubItem, body: str, status: WorkflowStatus | None
    ) -> CommentWritten:
        """Comment on ``item``'s conversation; a status is said to its author.

        The item is read before the write, never after it: a read that fails
        after the comment landed would make a retry post it twice.
        """
        data = await self.fetch(item)
        if status is not None:
            body = _addressed(
                render_workflow_status_comment(body, status),
                str((data.get("user") or {}).get("login") or ""),
                self.person,
            )
        response = await (await self.client()).post(
            f"{item.conversation}/comments", json={"body": body.rstrip()}
        )
        written = response.json()
        return CommentWritten(
            id=comment_id(CONVERSATION, written.get("id")),
            url=written.get("html_url"),
            author=str((written.get("user") or {}).get("login") or ""),
            created_at=written.get("created_at"),
            target=self.target(item, data),
        )

    async def react(
        self, item: GitHubItem, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        """React to the comment or review of ``item`` named ``target_id``.

        Raises:
            ServiceUnsupported: If ``reaction`` is no meaning a reaction has.
        """
        if reaction not in _REACTIONS:
            raise ServiceUnsupported(f"Unsupported semantic reaction: {reaction}")
        content = _REACTIONS[reaction]
        await self.fetch(item)
        node = await self.node_of(item, target_id)
        await self.graphql(ADD_REACTION, {"subject": node, "content": content})
        return ReactionAdded(reaction=reaction, id=target_id)

    async def node_of(self, item: GitHubItem, target_id: str) -> str:
        """The node of what ``item``'s reads named ``target_id``.

        GitHub finds comments by their repository-wide number, so where one is
        must be read before it is acted on as the item's.

        Raises:
            ServiceNotFound: If it is none of ``item``'s.
        """
        repo = f"/repos/{item.repository}"
        if target_id.startswith(CONVERSATION):
            path = f"{repo}/issues/comments/{number_of(target_id, CONVERSATION)}"
            owner_field, owner_api = "issue_url", item.conversation
        elif item.kind == "pull_request" and target_id.startswith(INLINE):
            path = f"{repo}/pulls/comments/{number_of(target_id, INLINE)}"
            owner_field, owner_api = "pull_request_url", item.api
        elif item.kind == "pull_request" and target_id.startswith(REVIEW):
            number = number_of(target_id, REVIEW)
            path = f"{item.api}/reviews/{number}"
            owner_field, owner_api = "pull_request_url", item.api
        else:
            raise ServiceNotFound(f"'{target_id}' is none of {self.url(item)}.")
        found = await self.get(path)
        # GitHub answers with the repository's own spelling of its name.
        if not isinstance(found, dict) or (
            str(found.get(owner_field)).casefold()
            != f"{self.base_url}{owner_api}".casefold()
        ):
            raise ServiceNotFound(f"'{target_id}' is none of {self.url(item)}.")
        return str(found["node_id"])


class GraphQLUnprocessable(ServiceError):
    """GitHub rejected the operation because its preconditions do not hold."""


def graphql_error(errors: Any) -> ServiceError:
    """What GraphQL ``errors`` say, as the ports' failure."""
    kinds = {item.get("type") for item in errors or [] if isinstance(item, dict)}
    if "RATE_LIMITED" in kinds:
        return ServiceError(t("integrations.github.response.rate_limit"))
    if kinds & {"FORBIDDEN", "INSUFFICIENT_SCOPES"}:
        return ServiceError(t("integrations.github.response.forbidden"))
    if "NOT_FOUND" in kinds:
        return ServiceNotFound(t("integrations.github.response.not_found"))
    if "UNPROCESSABLE" in kinds:
        return GraphQLUnprocessable(t("integrations.github.response.response"))
    return ServiceError(t("integrations.github.response.response"))


def _addressed(comment: str, author: str, person: Person) -> str:
    """``comment`` with a mention of the item's author, unless the member is
    the author or the comment already mentions them (logins are
    case-insensitive)."""
    if not author or member_identity(person).matches(author):
        return comment
    mention = r"(^|[^A-Za-z0-9_])@" + re.escape(author) + r"(?=$|[^A-Za-z0-9-])"
    if re.search(mention, comment, flags=re.IGNORECASE):
        return comment
    return f"@{author}\n\n{comment}"
