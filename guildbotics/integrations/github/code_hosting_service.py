"""GitHub translation for the code-hosting read contract."""

import json
from typing import Any

import httpx
from pydantic import ValidationError

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.code_hosting_service import (
    MAX_PAGE_BYTES,
    CodeHostingService,
    DependencyAlert,
    DependencyAlertQuery,
    RepositoryReadPage,
)
from guildbotics.integrations.github.github_utils import create_github_client
from guildbotics.integrations.github.read_resources import (
    GitHubReadError,
    prepare_read,
    read_page,
)
from guildbotics.integrations.github.repository_scope import configured_owner
from guildbotics.utils.i18n_tool import t


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
            raise GitHubReadError(t("integrations.github.read.resource"))
        conditions = parameters if parameters is not None else {}
        if not identifier:
            try:
                query = DependencyAlertQuery.model_validate(conditions)
            except ValidationError:
                raise GitHubReadError(
                    t("integrations.github.read.parameters")
                ) from None
            conditions = {
                "state": {
                    "open": "open",
                    "resolved": "fixed",
                    "dismissed": "dismissed,auto_dismissed",
                }[query.state],
                "per_page": query.page_size,
            }
        definition, request = prepare_read(
            "dependabot-alert" if identifier else "dependabot-alerts",
            repo,
            identifier,
            json.dumps(conditions),
        )
        if self._client is None:
            self._client = await create_github_client(
                self.person,
                self.base_url,
                self.owner,
                max_response_bytes=MAX_PAGE_BYTES,
            )
        page = await read_page(self._client, definition, request, continuation)
        try:
            result = RepositoryReadPage(
                items=[
                    _alert(item)
                    for item in ([page["data"]] if identifier else page["data"])
                ],
                continuation=page["continuation"],
            )
        except (ValidationError, AttributeError, TypeError, ValueError, KeyError):
            raise GitHubReadError(t("integrations.github.read.response")) from None
        return result

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


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
