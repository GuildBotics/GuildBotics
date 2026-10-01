"""GitHub routes and response translation for repository reads."""

import base64
import binascii
import json
import re
from http import HTTPStatus
from typing import Any

import httpx
from pydantic import Field

from guildbotics.integrations.code_hosting_service import (
    MAX_PAGE_BYTES,
    CodeHostingService,
    DependencyAlert,
    DependencyAlertQuery,
    ReadModel,
    RepositoryReadError,
    RepositoryReadPage,
)
from guildbotics.integrations.github.async_client import ResponseTooLarge
from guildbotics.integrations.github.pull_requests import (
    GitHubPullRequests,
    GitHubResource,
    MemberCapabilityError,
    _work_target,
)
from guildbotics.integrations.github.read_resources import (
    DETAIL_RESOURCES,
    GRAPH_RESOURCES,
    REST_RESOURCES,
    graph_connection,
    graph_query,
    translate,
)
from guildbotics.utils.i18n_tool import t


class PageQuery(ReadModel):
    page_size: int = Field(default=30, ge=1, le=100)
    node: str = Field(default="", max_length=256)


class ReadinessQuery(ReadModel):
    failed_logs: bool = False
    log_tail_bytes: int = Field(default=8192, ge=1, le=65536)


_CURSOR_LIMIT = 2048
_CONTINUATION_LIMIT = 8192


class GitHubCodeHostingService(GitHubPullRequests, CodeHostingService):
    _max_response_bytes = MAX_PAGE_BYTES

    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        if resource not in {
            "dependency_alerts",
            "pull_request_readiness",
            *REST_RESOURCES,
            *GRAPH_RESOURCES,
        }:
            raise RepositoryReadError(t("integrations.github.read.resource"))
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo
        ) or repo.rsplit("/", maxsplit=1)[-1] in {".", ".."}:
            raise RepositoryReadError(t("integrations.github.read.repository"))
        if identifier and not re.fullmatch(r"[1-9][0-9]{0,19}", identifier):
            raise RepositoryReadError(t("integrations.github.read.identifier"))
        if resource != "dependency_alerts" and not identifier:
            raise RepositoryReadError(t("integrations.github.read.identifier"))
        try:
            if parameters is not None and not isinstance(parameters, dict):
                raise ValueError
            if resource == "dependency_alerts":
                conditions = _parameters(identifier, parameters)
            elif resource == "pull_request_readiness":
                conditions = ReadinessQuery.model_validate(
                    parameters or {}
                ).model_dump()
            elif resource in DETAIL_RESOURCES:
                if parameters not in (None, {}):
                    raise ValueError
                conditions = {}
            else:
                conditions = PageQuery.model_validate(
                    parameters if parameters is not None else {}
                ).model_dump()
                if resource == "review_thread_comments":
                    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", conditions["node"]):
                        raise ValueError
                elif conditions["node"]:
                    raise ValueError
        except ValueError:
            raise RepositoryReadError(
                t("integrations.github.read.parameters")
            ) from None
        request = {
            "resource": resource,
            "repo": repo,
            "identifier": identifier,
            "parameters": conditions,
            "api_base_url": self.base_url,
        }
        if resource != "dependency_alerts":
            return await self._read_resource(request, continuation)
        query = dict(conditions)
        if continuation:
            if identifier:
                raise RepositoryReadError(t("integrations.github.read.continuation"))
            query["after"] = _cursor(continuation, request)
        path = f"repos/{repo}/dependabot/alerts"
        if identifier:
            path += f"/{identifier}"
        client = await self._get_client()
        try:
            response = await client.get(path, params=query, follow_redirects=False)
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

    async def _read_resource(
        self, request: dict[str, Any], continuation: str
    ) -> RepositoryReadPage:
        resource, repo, identifier, conditions = (
            request[k] for k in ("resource", "repo", "identifier", "parameters")
        )
        if continuation and resource in DETAIL_RESOURCES:
            raise RepositoryReadError(t("integrations.github.read.continuation"))
        cursor = _cursor(continuation, request) if continuation else ""
        if (
            resource in REST_RESOURCES
            and cursor
            and not re.fullmatch(r"[1-9][0-9]{0,5}", cursor)
        ):
            raise RepositoryReadError(t("integrations.github.read.continuation"))
        client = await self._get_client()
        try:
            if resource == "pull_request_readiness":
                result = await self.pr_checks(
                    f"{self.web_base_url()}/{repo}/pull/{identifier}", **conditions
                )
                return RepositoryReadPage(items=[result], target=result["target"])
            next_cursor = None
            if resource in GRAPH_RESOURCES:
                owner, name = repo.split("/")
                variables = {
                    "owner": owner,
                    "repo": name,
                    "number": int(identifier),
                    "after": cursor or None,
                    "size": conditions["page_size"],
                }
                if resource == "review_thread_comments":
                    variables["node"] = conditions["node"]
                response = await client.post(
                    "graphql",
                    json={"query": graph_query(resource), "variables": variables},
                    follow_redirects=False,
                )
                if response.status_code != HTTPStatus.OK:
                    raise _http_error(response)
                payload = response.json()
                if payload.get("errors"):
                    raise ValueError
                connection = graph_connection(resource, payload["data"])
                data = connection["nodes"]
                info = connection["pageInfo"]
                if info["hasNextPage"]:
                    next_cursor = info["endCursor"]
                    if (
                        not isinstance(next_cursor, str)
                        or not 0 < len(next_cursor) <= _CURSOR_LIMIT
                        or next_cursor == cursor
                    ):
                        raise ValueError
            else:
                path = f"repos/{repo}/" + REST_RESOURCES[resource].format(
                    identifier=identifier
                )
                params = (
                    {}
                    if resource in DETAIL_RESOURCES
                    else {"per_page": conditions["page_size"], "page": cursor or "1"}
                )
                response = await client.get(path, params=params, follow_redirects=False)
                if response.status_code != HTTPStatus.OK:
                    raise _http_error(response)
                data = response.json()
                if resource in DETAIL_RESOURCES:
                    data = [data]
                elif response.links.get("next"):
                    pages = httpx.URL(response.links["next"]["url"]).params.get_list(
                        "page"
                    )
                    if (
                        len(pages) != 1
                        or not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0])
                        or int(pages[0]) <= int(cursor or "1")
                    ):
                        raise ValueError
                    next_cursor = pages[0]
            if not isinstance(data, list) or any(
                not isinstance(item, dict) for item in data
            ):
                raise ValueError
            target = None
            if resource in {"issues", "pull_requests"}:
                item = data[0]
                if item.get("number") != int(identifier) or not isinstance(
                    item.get("title"), str
                ):
                    raise ValueError
                owner, name = repo.split("/")
                ref = GitHubResource(
                    owner,
                    name,
                    int(identifier),
                    "pull" if resource == "pull_requests" else "issue",
                )
                target = _work_target(ref, item)
                result = {
                    **target,
                    "body": item.get("body") or "",
                    "state": item["state"],
                    "assignees": [v["login"] for v in item.get("assignees", [])],
                    "labels": [v["name"] for v in item.get("labels", [])],
                }
                if resource == "pull_requests":
                    head = self._pull_request_head(ref, item)
                    result.update(
                        head=head.branch,
                        head_repo=head.full_repo,
                        head_owner=head.owner,
                        head_repo_name=head.repo,
                        head_sha=self._pull_request_head_sha(ref, item),
                        base=(item.get("base") or {}).get("ref", ""),
                        merged=item.get("merged_at") is not None,
                        draft=bool(item.get("draft")),
                        changed_files=item.get("changed_files"),
                    )
                data = [result]
            return RepositoryReadPage(
                items=[translate(resource, item, self.person) for item in data],
                target=target,
                continuation=_encode_cursor(next_cursor, request)
                if next_cursor
                else None,
            )
        except ResponseTooLarge:
            raise RepositoryReadError(t("integrations.github.read.too_large")) from None
        except MemberCapabilityError as exc:
            raise RepositoryReadError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise _http_error(exc.response) from None
        except httpx.RequestError:
            raise RepositoryReadError(t("integrations.github.read.transport")) from None
        except (AttributeError, TypeError, ValueError, KeyError):
            raise RepositoryReadError(t("integrations.github.read.response")) from None


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


def _encode_cursor(cursor: str, request: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": cursor}).encode()
    ).decode()
