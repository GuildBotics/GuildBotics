from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from guildbotics.capabilities import github_activity_events
from guildbotics.capabilities.github_activity_events import (
    GitHubActivityEventPoller,
    _closed_event,
    _existing_activity_ids,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.activity_event_store import ActivityEventStore

ISSUE_NUMBER = 8


def test_closed_project_items_become_shared_github_activity_events():
    merged = _closed_event(
        {
            "__typename": "PullRequest",
            "number": 7,
            "title": "Merged",
            "url": "https://github.com/acme/demo/pull/7",
            "state": "CLOSED",
            "closedAt": "2026-07-10T01:00:00Z",
            "mergedAt": "2026-07-10T01:00:00Z",
            "repository": {"name": "demo", "owner": {"login": "acme"}},
        }
    )
    issue = _closed_event(
        {
            "__typename": "Issue",
            "number": ISSUE_NUMBER,
            "title": "Done",
            "url": "https://github.com/acme/demo/issues/8",
            "state": "CLOSED",
            "closedAt": "2026-07-10T02:00:00Z",
            "repository": {"name": "demo", "owner": {"login": "acme"}},
        }
    )

    assert merged is not None and merged["payload"]["pull_request"]["merged"] is True
    assert merged["activity_id"] == "pull_request:acme/demo:7:merged"
    assert issue is not None and issue["payload"]["issue"]["number"] == ISSUE_NUMBER
    assert issue["activity_id"] == "issue:acme/demo:8:closed"


@pytest.mark.parametrize(
    "override",
    [
        {"repository": {"name": "", "owner": {"login": ""}}},
        {"number": None},
        {"number": 0},
        {"url": ""},
        {"closedAt": "not-a-date"},
        {"closedAt": "2026-07-10T01:00:00"},
    ],
)
def test_closed_event_ignores_incomplete_or_invalid_items(override):
    item = {
        "__typename": "Issue",
        "number": ISSUE_NUMBER,
        "title": "Done",
        "url": "https://github.com/acme/demo/issues/8",
        "state": "CLOSED",
        "closedAt": "2026-07-10T01:00:00Z",
        "repository": {"name": "demo", "owner": {"login": "acme"}},
    }
    item.update(override)

    assert (
        _closed_event(
            item,
            datetime(2026, 7, 10, tzinfo=UTC),
            datetime(2026, 7, 11, tzinfo=UTC),
        )
        is None
    )


def test_existing_activity_ids_reads_shared_activity_events(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    activity_id = "issue:acme/demo:8:closed"
    store = ActivityEventStore()
    store.record(
        {
            "type": "github.issue.closed",
            "timestamp": "2026-07-10T01:00:00Z",
            "attributes": {"github.activity_id": activity_id},
        }
    )
    for index in range(20):
        store.record(
            {
                "type": "github.issue.closed",
                "timestamp": "2026-07-10T02:00:00Z",
                "attributes": {"filler": str(index)},
            }
        )
    store.record(
        {
            "type": "github.issue.closed",
            "timestamp": "2026-07-12T01:00:00Z",
            "attributes": {"github.activity_id": "issue:acme/demo:9:closed"},
        }
    )

    existing = _existing_activity_ids(
        datetime(2026, 7, 10, tzinfo=UTC),
        datetime(2026, 7, 11, tzinfo=UTC),
    )

    assert activity_id in existing
    # Out-of-range events do not count as existing.
    assert "issue:acme/demo:9:closed" not in existing


@pytest.mark.asyncio
async def test_poller_records_each_closed_project_item_once(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    team = Team(
        project=Project(
            services={
                "ticket_manager": {
                    "name": "GitHub",
                    "owner": "acme",
                    "project_id": "1",
                    "url": "https://github.com/orgs/acme/projects/1",
                }
            }
        ),
        members=[person],
    )

    queries: list[str] = []

    class Client:
        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(json=lambda: [], raise_for_status=lambda: None)

        async def post(self, *_args, **kwargs):
            queries.append(kwargs["json"]["query"])
            return SimpleNamespace(
                json=lambda: {
                    "data": {
                        "organization": {
                            "projectV2": {
                                "items": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "content": {
                                                "__typename": "PullRequest",
                                                "number": 7,
                                                "title": "Merged",
                                                "url": "https://github.com/acme/demo/pull/7",
                                                "state": "CLOSED",
                                                "closedAt": "2026-07-10T01:00:00Z",
                                                "mergedAt": "2026-07-10T01:00:00Z",
                                                "repository": {
                                                    "name": "demo",
                                                    "owner": {"login": "acme"},
                                                },
                                            }
                                        }
                                    ],
                                }
                            }
                        }
                    }
                },
                raise_for_status=lambda: None,
            )

        async def aclose(self):
            return None

    async def fake_client(*_args, **_kwargs):
        return Client()

    monkeypatch.setattr(github_activity_events, "create_github_client", fake_client)
    poller = GitHubActivityEventPoller(team, person)

    start = datetime(2026, 7, 10, tzinfo=UTC)
    end = datetime(2026, 7, 11, tzinfo=UTC)
    assert await poller.poll(start, end) == 1
    assert await poller.poll(start, end) == 0
    assert queries
    normalized_query = " ".join(queries[0].split())
    assert "items(first:100, after:$cursor) { nodes { content {" in normalized_query
    assert "} } pageInfo { hasNextPage endCursor } }" in normalized_query
    records = ActivityEventStore().records_between(
        datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 12, tzinfo=UTC)
    )
    assert len(records) == 1
    assert records[0]["person_id"] == ""
    assert records[0]["timestamp"] == "2026-07-10T01:00:00Z"
    assert (
        records[0]["attributes"]["github.activity_id"]
        == "pull_request:acme/demo:7:merged"
    )
    assert records[0]["attributes"]["github.number"] == "7"
