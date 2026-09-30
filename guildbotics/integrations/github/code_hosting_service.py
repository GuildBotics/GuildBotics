"""GitHub routes and response translation for repository reads."""

import base64
import binascii
import json
import re
from http import HTTPStatus
from typing import Any

import httpx

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.code_hosting_service import (
    MAX_PAGE_BYTES,
    CodeHostingService,
    DependencyAlert,
    DependencyAlertQuery,
    RepositoryReadError,
    RepositoryReadPage,
)
from guildbotics.integrations.github.async_client import ResponseTooLarge
from guildbotics.integrations.github.github_utils import create_github_client
from guildbotics.integrations.github.repository_scope import configured_owner
from guildbotics.utils.i18n_tool import t

_CURSOR_LIMIT = 2048
_CONTINUATION_LIMIT = 8192


class GitHubCodeHostingService(CodeHostingService):
    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.owner = configured_owner(team.project)
        config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.base_url = str(config.get("api_base_url") or "https://api.github.com")
        self._client: httpx.AsyncClient | None = None

    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        if resource != "dependency_alerts":
            raise RepositoryReadError(t("integrations.github.read.resource"))
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo
        ) or repo.rsplit("/", maxsplit=1)[-1] in {".", ".."}:
            raise RepositoryReadError(t("integrations.github.read.repository"))
        if identifier and not re.fullmatch(r"[1-9][0-9]{0,19}", identifier):
            raise RepositoryReadError(t("integrations.github.read.identifier"))
        conditions = _parameters(identifier, parameters)
        request = {
            "resource": resource,
            "repo": repo,
            "identifier": identifier,
            "parameters": conditions,
            "api_base_url": self.base_url,
        }
        query = dict(conditions)
        if continuation:
            if identifier:
                raise RepositoryReadError(t("integrations.github.read.continuation"))
            query["after"] = _cursor(continuation, request)
        path = f"repos/{repo}/dependabot/alerts"
        if identifier:
            path += f"/{identifier}"
        if self._client is None:
            self._client = await create_github_client(
                self.person,
                self.base_url,
                self.owner,
                max_response_bytes=MAX_PAGE_BYTES,
            )
        try:
            response = await self._client.get(
                path, params=query, follow_redirects=False
            )
            if response.status_code != HTTPStatus.OK:
                raise _http_error(response)
            data = response.json()
            if identifier:
                data = [data]
            if not isinstance(data, list):
                raise ValueError
            return RepositoryReadPage(
                items=[_alert(item) for item in data],
                continuation=None if identifier else _continuation(response, request),
            )
        except ResponseTooLarge:
            raise RepositoryReadError(t("integrations.github.read.too_large")) from None
        except httpx.HTTPStatusError as exc:
            raise _http_error(exc.response) from None
        except httpx.RequestError:
            raise RepositoryReadError(t("integrations.github.read.transport")) from None
        except (AttributeError, TypeError, ValueError, KeyError):
            raise RepositoryReadError(t("integrations.github.read.response")) from None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parameters(identifier: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    try:
        if identifier:
            if parameters not in (None, {}):
                raise ValueError
            return {}
        query = DependencyAlertQuery.model_validate(
            parameters if parameters is not None else {}
        )
        return {
            "state": {
                "open": "open",
                "resolved": "fixed",
                "dismissed": "dismissed,auto_dismissed",
            }[query.state],
            "per_page": query.page_size,
        }
    except ValueError:
        raise RepositoryReadError(t("integrations.github.read.parameters")) from None


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
        raise RepositoryReadError(t("integrations.github.read.continuation")) from None


def _continuation(response: httpx.Response, request: dict[str, Any]) -> str | None:
    link = response.links.get("next")
    if link is None:
        return None
    try:
        # GitHub may canonicalize the URL to /repositories/{id}/..., or omit
        # filters. Only the cursor is data we need; we never request this URL.
        cursors = httpx.URL(link["url"]).params.get_list("after")
        if len(cursors) != 1 or not 0 < len(cursors[0]) <= _CURSOR_LIMIT:
            raise ValueError
    except (KeyError, ValueError, httpx.InvalidURL):
        raise RepositoryReadError(t("integrations.github.read.continuation")) from None
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": cursors[0]}).encode()
    ).decode()


def _http_error(response: httpx.Response) -> RepositoryReadError:
    status = response.status_code
    if status == HTTPStatus.TOO_MANY_REQUESTS or (
        status == HTTPStatus.FORBIDDEN
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    ):
        return RepositoryReadError(t("integrations.github.read.rate_limit"))
    message = {
        401: t("integrations.github.read.authentication"),
        403: t("integrations.github.read.forbidden"),
        404: t("integrations.github.read.not_found"),
        422: t("integrations.github.read.rejected"),
    }.get(status, t("integrations.github.read.http", status=status))
    return RepositoryReadError(message)


def _alert(item: dict[str, Any]) -> DependencyAlert:
    number = item["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid alert identifier")
    dependency = item.get("dependency") or {}
    package = dependency.get("package") or {}
    advisory = item.get("security_advisory") or {}
    vulnerability = item.get("security_vulnerability") or {}
    patched = vulnerability.get("first_patched_version") or {}
    return DependencyAlert.model_validate(
        {
            "id": str(number),
            "state": {
                "open": "open",
                "fixed": "resolved",
                "dismissed": "dismissed",
                "auto_dismissed": "dismissed",
            }[item["state"]],
            "url": item.get("html_url"),
            "package": package.get("name"),
            "ecosystem": package.get("ecosystem"),
            "manifest_path": dependency.get("manifest_path"),
            "severity": vulnerability.get("severity") or advisory.get("severity"),
            "identifiers": advisory.get("identifiers") or [],
            "summary": advisory.get("summary"),
            "description": advisory.get("description"),
            "affected_versions": vulnerability.get("vulnerable_version_range"),
            "patched_version": patched.get("identifier"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
    )
