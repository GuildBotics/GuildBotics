"""Host-owned GitHub read resources, validation, and bounded page transport."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.integrations.code_hosting_service import (
    MAX_PAGE_BYTES,
    RepositoryReadError,
)
from guildbotics.integrations.github.async_client import ResponseTooLarge
from guildbotics.utils.i18n_tool import t

_CURSOR_LIMIT = 2048
_CONTINUATION_LIMIT = 8192


class GitHubReadError(RepositoryReadError):
    """A safe, localized failure that carries no upstream body or credentials."""


class NoParameters(BaseModel):
    """Reject all conditions unless a resource explicitly declares them."""

    model_config = ConfigDict(extra="forbid", strict=True)


class AlertParameters(NoParameters):
    """The conditions this host exposes for Dependabot collections."""

    state: Literal[
        "open", "fixed", "dismissed", "auto_dismissed", "dismissed,auto_dismissed"
    ] = "open"
    per_page: int = Field(default=30, ge=1, le=100)


@dataclass(frozen=True)
class ReadResource:
    """An allowed REST route and its input and page shape."""

    path: str
    parameters: type[NoParameters]
    collection: bool = False
    identifier_pattern: str = ""


RESOURCES = {
    "dependabot-alerts": ReadResource(
        "repos/{repo}/dependabot/alerts", AlertParameters, collection=True
    ),
    "dependabot-alert": ReadResource(
        "repos/{repo}/dependabot/alerts/{identifier}",
        NoParameters,
        identifier_pattern=r"[1-9][0-9]{0,19}",
    ),
}


def prepare_read(
    resource: str, repo: str, identifier: str, parameters: str
) -> tuple[ReadResource, dict[str, Any]]:
    """Validate a request before creating an authenticated client."""
    definition = RESOURCES.get(resource)
    if definition is None:
        raise GitHubReadError(t("integrations.github.read.resource"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo):
        raise GitHubReadError(t("integrations.github.read.repository"))
    if repo.split("/")[1] in {".", ".."}:
        raise GitHubReadError(t("integrations.github.read.repository"))
    if not re.fullmatch(definition.identifier_pattern, identifier):
        raise GitHubReadError(t("integrations.github.read.identifier"))
    try:
        conditions = definition.parameters.model_validate_json(parameters).model_dump()
    except ValidationError:
        raise GitHubReadError(t("integrations.github.read.parameters")) from None
    return definition, {
        "resource": resource,
        "repo": repo,
        "identifier": identifier,
        "parameters": conditions,
    }


def _cursor(continuation: str, request: dict[str, Any]) -> str:
    """A continuation is data, never a URL or an authorization grant."""
    try:
        if len(continuation) > _CONTINUATION_LIMIT:
            raise ValueError
        value = json.loads(
            base64.b64decode(continuation, altchars=b"-_", validate=True)
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"request", "after"}
            or value["request"] != request
            or not isinstance(value["after"], str)
            or not 0 < len(value["after"]) <= _CURSOR_LIMIT
        ):
            raise ValueError
        return value["after"]
    except (ValueError, binascii.Error, UnicodeError):
        raise GitHubReadError(t("integrations.github.read.continuation")) from None


def _continuation(
    response: httpx.Response, request: dict[str, Any], url: httpx.URL
) -> str | None:
    link = response.links.get("next")
    if link is None:
        return None
    try:
        target = httpx.URL(link["url"])
        pairs = target.params.multi_items()
        query = dict(pairs)
        after = query.pop("after")
        expected = {key: str(value) for key, value in request["parameters"].items()}
        if (
            target.copy_with(query=None) != url
            or len(pairs) != len(target.params)
            or query != expected
            or not 0 < len(after) <= _CURSOR_LIMIT
        ):
            raise ValueError
    except (KeyError, ValueError, httpx.InvalidURL):
        raise GitHubReadError(t("integrations.github.read.continuation")) from None
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": after}).encode()
    ).decode()


def _http_error(response: httpx.Response) -> GitHubReadError:
    status = response.status_code
    if status == HTTPStatus.TOO_MANY_REQUESTS or (
        status == HTTPStatus.FORBIDDEN
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    ):
        return GitHubReadError(t("integrations.github.read.rate_limit"))
    message = {
        401: t("integrations.github.read.authentication"),
        403: t("integrations.github.read.forbidden"),
        404: t("integrations.github.read.not_found"),
        422: t("integrations.github.read.rejected"),
    }.get(status, t("integrations.github.read.http", status=status))
    return GitHubReadError(message)


async def read_page(
    client: httpx.AsyncClient,
    definition: ReadResource,
    request: dict[str, Any],
    continuation: str = "",
) -> dict[str, Any]:
    """Read exactly one allowed page, without following upstream URLs."""
    request = {**request, "api_base_url": str(client.base_url)}
    parameters = dict(request["parameters"])
    if continuation:
        if not definition.collection:
            raise GitHubReadError(t("integrations.github.read.continuation"))
        parameters["after"] = _cursor(continuation, request)
    path = definition.path.format(
        repo=request["repo"], identifier=request["identifier"]
    )
    url = client.base_url.join(path)
    try:
        async with client.stream(
            "GET", url, params=parameters, follow_redirects=False
        ) as response:
            if response.status_code != HTTPStatus.OK:
                raise _http_error(response)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_PAGE_BYTES:
                    raise GitHubReadError(t("integrations.github.read.too_large"))
            try:
                data = json.loads(content)
            except (ValueError, UnicodeError):
                raise GitHubReadError(t("integrations.github.read.response")) from None
            if definition.collection:
                if not isinstance(data, list) or not all(
                    isinstance(item, dict) for item in data
                ):
                    raise GitHubReadError(t("integrations.github.read.response"))
                next_page = _continuation(response, request, url)
            else:
                if not isinstance(data, dict):
                    raise GitHubReadError(t("integrations.github.read.response"))
                next_page = None
    except ResponseTooLarge:
        raise GitHubReadError(t("integrations.github.read.too_large")) from None
    except httpx.HTTPStatusError as exc:
        raise _http_error(exc.response) from None
    except httpx.RequestError:
        raise GitHubReadError(t("integrations.github.read.transport")) from None
    result = {**request, "data": data, "continuation": next_page}
    if len(json.dumps(result).encode()) > MAX_PAGE_BYTES:
        raise GitHubReadError(t("integrations.github.read.too_large"))
    return result
