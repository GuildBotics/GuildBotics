"""A member's GitHub client: every request passes the write gate before it is
sent, and every failure comes out as the ports' failure
(:class:`~guildbotics.runtime.port.ServiceError`), told from the status alone.

An error answer is turned into one here, in the client's response hook, so a
provider never sees an error response; one that a call accepts (removing a
label the issue does not carry) catches :class:`GitHubNotFound`.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx

from guildbotics.integrations.github.repository_scope import check_request
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.utils.i18n_tool import t


class ResponseTooLarge(httpx.RequestError):
    """A decoded response exceeded the request's byte limit."""


class GitHubResponseError(ServiceError):
    """GitHub answered with an error; the message says what its status means
    and nothing of what GitHub wrote."""

    def __init__(self, status: int, *, rate_limited: bool, permission: str = ""):
        self.status = status
        self.rate_limited = rate_limited
        super().__init__(_message(status, rate_limited, permission))

    @classmethod
    def of(cls, response: httpx.Response) -> GitHubResponseError:
        status = response.status_code
        rate_limited = status == HTTPStatus.TOO_MANY_REQUESTS or (
            status == HTTPStatus.FORBIDDEN
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
        )
        kind = GitHubNotFound if status == HTTPStatus.NOT_FOUND else cls
        return kind(status, rate_limited=rate_limited)

    def needing(self, permission: str) -> GitHubResponseError:
        """The same failure, told with the permission the request needed."""
        return type(self)(
            self.status, rate_limited=self.rate_limited, permission=permission
        )


class GitHubNotFound(GitHubResponseError, ServiceNotFound):
    """GitHub has no such resource, or does not show it to the member."""


def _message(status: int, rate_limited: bool, permission: str) -> str:
    if rate_limited:
        return t("integrations.github.response.rate_limit")
    if status == HTTPStatus.UNAUTHORIZED:
        return t("integrations.github.response.authentication")
    if status in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND):
        message = (
            t("integrations.github.response.forbidden")
            if status == HTTPStatus.FORBIDDEN
            else t("integrations.github.response.not_found")
        )
        if permission:
            message += " " + t(
                "integrations.github.response.permission", permission=permission
            )
        return message
    if status == HTTPStatus.UNPROCESSABLE_ENTITY:
        return t("integrations.github.response.rejected")
    return t("integrations.github.response.http", status=status)


async def _read_bounded(response: httpx.Response, limit: int) -> None:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > limit:
            raise ResponseTooLarge(
                "Response exceeds byte limit", request=response.request
            )
        content.extend(chunk)
    # Populate HTTPX's read cache so auth refresh and error hooks can read the
    # same bounded body. Auth's requires_response_body runs after this hook.
    response._content = bytes(content)


def record_github_auth_failure(
    *, person_id: str = "", code: str = "unauthorized"
) -> None:
    record_correlated_event(
        event_type="credential.failed",
        default_source="github",
        person_id=person_id,
        attributes={
            "credential.provider": "github",
            "error.category": "authentication",
        },
        payload={"provider": "github", "code": code},
    )


def check_response(response: httpx.Response, *, person_id: str = "") -> None:
    """Raises:
    GitHubResponseError: If ``response`` is an error, after recording a
        refused credential.
    """
    if not response.is_error:
        return
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        record_github_auth_failure(person_id=person_id)
    raise GitHubResponseError.of(response)


class _MemberClient(httpx.AsyncClient):
    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except ResponseTooLarge:
            raise ServiceError(t("integrations.github.response.too_large")) from None
        except httpx.HTTPStatusError as exc:
            # An app's token renewal that GitHub refused, in the auth flow.
            raise GitHubResponseError.of(exc.response) from None
        except httpx.RequestError:
            raise ServiceError(t("integrations.github.response.transport")) from None


def get_app_client(base_url: str) -> httpx.AsyncClient:
    """A client of the GitHub API at ``base_url`` for a GitHub App itself,
    which authenticates each request on its own: it renews an installation's
    token, and writes nothing."""

    async def response_hook(response: httpx.Response) -> None:
        check_response(response)

    return _MemberClient(
        base_url=base_url, timeout=10.0, event_hooks={"response": [response_hook]}
    )


def get_async_client(base_url: str, auth: httpx.Auth, owner: str) -> httpx.AsyncClient:
    """A member's client of the GitHub API at ``base_url``.

    Args:
        base_url: The API's base URL.
        auth: How the member authenticates.
        owner: The configured owner, the only one whose repositories the
            client writes to.
    """
    base = httpx.URL(base_url)
    # An app's auth flow renews its token on a 401 and retries: that 401 is
    # not the answer.
    retries_unauthorized = bool(getattr(auth, "handles_unauthorized", False))
    person_id = str(getattr(auth, "person_id", ""))

    async def request_hook(request: httpx.Request) -> None:
        await check_request(owner, base, request, client)

    async def response_hook(response: httpx.Response) -> None:
        max_response_bytes = response.request.extensions.get("max_response_bytes")
        if max_response_bytes is not None:
            await _read_bounded(response, max_response_bytes)
        if retries_unauthorized and response.status_code == HTTPStatus.UNAUTHORIZED:
            return
        check_response(response, person_id=person_id)

    client = _MemberClient(
        base_url=base_url,
        auth=auth,
        timeout=10.0,
        event_hooks={
            "request": [request_hook],
            "response": [response_hook],
        },
    )
    return client
