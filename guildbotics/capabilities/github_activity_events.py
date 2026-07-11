"""Import completed GitHub Project work as shared activity events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.github_utils import create_github_client
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.diagnostics_store import DiagnosticsStore

ACTIVITY_DUPLICATE_SCAN_LIMIT = 50_000


class GitHubActivityEventPoller:
    """Poll the configured GitHub Project and persist closed work once.

    Project items are the configured scope of GuildBotics work, so this avoids
    inventing a repository list or subscribing to unrelated organization events.
    """

    def __init__(self, team: Team, person: Person) -> None:
        self._team = team
        self._person = person

    @classmethod
    def is_configured(cls, team: Team) -> bool:
        config = team.project.get_service_config(Service.TICKET_MANAGER)
        return (
            team.project.get_service_name(Service.TICKET_MANAGER) == "github"
            and bool(config.get("owner"))
            and bool(config.get("project_id"))
            and bool(config.get("url"))
        )

    async def poll(self, start: datetime, end: datetime) -> int:
        if not self.is_configured(self._team):
            return 0
        config = self._team.project.get_service_config(Service.TICKET_MANAGER)
        client = await create_github_client(
            self._person, str(config.get("base_url", "https://api.github.com"))
        )
        try:
            items = await self._project_items(client, config)
            pull_requests = await self._closed_pull_requests(client, items)
        finally:
            await client.aclose()
        existing = _existing_activity_ids(start, end)
        recorded = 0
        for item in [*items, *pull_requests]:
            event = _closed_event(item, start, end)
            if event is None or event["activity_id"] in existing:
                continue
            record_correlated_event(
                event_type=event["event_type"],
                payload=event["payload"],
                attributes={
                    "github.action": "closed",
                    "github.kind": event["kind"],
                    "github.number": event["number"],
                    "github.url": event["url"],
                    "github.repo": event["repo"],
                    "github.activity_id": event["activity_id"],
                },
                default_source="github",
                timestamp=event["timestamp"],
            )
            existing.add(event["activity_id"])
            recorded += 1
        return recorded

    async def _project_items(
        self, client: Any, config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        project_type = "organization" if "/orgs/" in str(config["url"]) else "user"
        query = f"""
        query($owner:String!, $number:Int!, $cursor:String) {{
          {project_type}(login:$owner) {{
            projectV2(number:$number) {{
              items(first:100, after:$cursor) {{
                nodes {{ content {{
                  __typename
                  ... on Issue {{ number title url state closedAt repository {{ name owner {{ login }} }} }}
                  ... on PullRequest {{ number title url state closedAt mergedAt repository {{ name owner {{ login }} }} }}
                }} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
          }}
        }}
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await client.post(
                "/graphql",
                json={
                    "query": query,
                    "variables": {
                        "owner": config["owner"],
                        "number": int(str(config["project_id"])),
                        "cursor": cursor,
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            project = ((payload.get("data") or {}).get(project_type) or {}).get(
                "projectV2"
            ) or {}
            connection = project.get("items") or {}
            items.extend(
                item.get("content") or {} for item in connection.get("nodes") or []
            )
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return items
            cursor = str(page["endCursor"])

    async def _closed_pull_requests(
        self, client: Any, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        repositories = {_repository_name(item) for item in items}
        pull_requests: list[dict[str, Any]] = []
        for repository in sorted(
            repository for repository in repositories if repository
        ):
            response = await client.get(
                f"/repos/{repository}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(
                    f"Unexpected GitHub pull request response for {repository}"
                )
            for pull_request in payload:
                if not isinstance(pull_request, dict) or not pull_request.get(
                    "closed_at"
                ):
                    continue
                pull_requests.append(
                    {
                        "__typename": "PullRequest",
                        "number": pull_request.get("number"),
                        "title": pull_request.get("title"),
                        "url": pull_request.get("html_url"),
                        "state": "CLOSED",
                        "closedAt": pull_request.get("closed_at"),
                        "mergedAt": pull_request.get("merged_at"),
                        "repository": _repository_parts(repository),
                    }
                )
        return pull_requests


def _closed_event(
    item: dict[str, Any], start: datetime | None = None, end: datetime | None = None
) -> dict[str, Any] | None:
    if item.get("state") != "CLOSED" or not item.get("closedAt"):
        return None
    repo = _repository_name(item)
    if not repo:
        return None
    number = item.get("number")
    if not isinstance(number, int) or number <= 0:
        return None
    url = str(item.get("url") or "")
    if not url:
        return None
    merged_at = item.get("mergedAt")
    timestamp = str(merged_at or item["closedAt"])
    occurred = _parse_github_timestamp(timestamp)
    if occurred is None:
        return None
    if start and end and not start <= occurred <= end:
        return None
    is_pull_request = item.get("__typename") == "PullRequest"
    kind = "pull_request" if is_pull_request else "issue"
    event_type = "github.pull_request" if is_pull_request else "github.issue"
    activity = "merged" if merged_at else "closed"
    return {
        "activity_id": f"{kind}:{repo}:{number}:{activity}",
        "event_type": event_type,
        "kind": kind,
        "number": number,
        "url": url,
        "repo": repo,
        "timestamp": timestamp,
        "payload": {
            "action": "closed",
            kind: {
                "number": number,
                "title": str(item.get("title") or ""),
                "html_url": url,
                "merged": bool(merged_at),
            },
        },
    }


def _repository_name(item: dict[str, Any]) -> str:
    repository = cast(dict[str, Any], item.get("repository") or {})
    owner = cast(dict[str, Any], repository.get("owner") or {})
    return "/".join(
        part
        for part in (str(owner.get("login") or ""), str(repository.get("name") or ""))
        if part
    )


def _repository_parts(repository: str) -> dict[str, Any]:
    owner, name = repository.split("/", maxsplit=1)
    return {"name": name, "owner": {"login": owner}}


def _parse_github_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _timestamp_between(timestamp: str, start: datetime, end: datetime) -> bool:
    occurred = _parse_github_timestamp(timestamp)
    return occurred is not None and start <= occurred <= end


def _existing_activity_ids(start: datetime, end: datetime) -> set[str]:
    return {
        str(attributes.get("github.activity_id"))
        for item in DiagnosticsStore(
            memory_limit=ACTIVITY_DUPLICATE_SCAN_LIMIT
        ).records_between(
            includes=lambda timestamp: _timestamp_between(timestamp, start, end),
            limit=ACTIVITY_DUPLICATE_SCAN_LIMIT,
        )
        if isinstance((attributes := item.get("attributes")), dict)
        and attributes.get("github.activity_id")
    }


async def refresh_github_activity_events(
    team: Team, start: datetime, end: datetime
) -> int:
    """Refresh shared GitHub activity using one configured active member."""
    person = next(
        (
            member
            for member in team.members
            if (
                member.has_secret("GITHUB_ACCESS_TOKEN")
                or member.has_secret("GITHUB_APP_ID")
            )
        ),
        None,
    )
    if person is None or not GitHubActivityEventPoller.is_configured(team):
        return 0
    return await GitHubActivityEventPoller(team, person).poll(start, end)
