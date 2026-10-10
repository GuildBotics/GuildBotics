"""The GitHub Project's closed work: its own closed items, and the closed pull
requests of the repositories it holds items of."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github.github_ticket_manager import (
    GitHubTicketManager,
    _closed_item,
)

PAGE_SIZE = 100
START = datetime(2026, 7, 10, tzinfo=UTC)
END = datetime(2026, 7, 11, tzinfo=UTC)
_ISSUE = {
    "__typename": "Issue",
    "number": 8,
    "title": "Done",
    "url": "https://github.com/acme/demo/issues/8",
    "state": "CLOSED",
    "closedAt": "2026-07-10T01:00:00Z",
    "repository": {"name": "demo", "owner": {"login": "acme"}},
}


def _manager(client) -> GitHubTicketManager:
    person = Person(
        person_id="aiko", name="Aiko", account_info={"github_username": "aiko"}
    )
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
    manager = GitHubTicketManager(logging.getLogger(__name__), person, team)
    manager.client = client
    manager._project_node_id = "PROJECT"
    return manager


def test_a_closed_item_is_named_by_its_merge_or_close():
    merged = _closed_item(
        {
            **_ISSUE,
            "__typename": "PullRequest",
            "number": 7,
            "url": "https://github.com/acme/demo/pull/7",
            "mergedAt": "2026-07-10T02:00:00Z",
        }
    )
    issue = _closed_item(_ISSUE)

    assert merged is not None and (merged.kind, merged.merged) == ("pull_request", True)
    assert merged.closed_at == "2026-07-10T02:00:00Z"
    assert issue is not None and (issue.kind, issue.number, issue.merged) == (
        "issue",
        8,
        False,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"repository": {"name": "", "owner": {"login": ""}}},
        {"number": None},
        {"number": 0},
        {"url": ""},
        {"state": "OPEN"},
        {"closedAt": None},
    ],
)
def test_an_incomplete_or_open_item_is_no_closed_work(override):
    assert _closed_item({**_ISSUE, **override}) is None


@pytest.mark.parametrize("closed_at", ["not-a-date", "2026-07-10T01:00:00"])
@pytest.mark.asyncio
async def test_closed_since_keeps_the_window_and_drops_unreadable_times(closed_at):
    class Client:
        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(json=lambda: [], raise_for_status=lambda: None)

        async def post(self, *_args, **_kwargs):
            items = [
                {"content": _ISSUE},
                {"content": {**_ISSUE, "number": 9, "closedAt": closed_at}},
                {"content": {**_ISSUE, "number": 10, "closedAt": "2026-07-12T00:00Z"}},
            ]
            data = {
                "node": {
                    "items": {
                        "nodes": items,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": data},
                raise_for_status=lambda: None,
            )

    closed = await _manager(Client()).closed_since(START, END)

    assert [item.number for item in closed] == [8]


@pytest.mark.asyncio
async def test_closed_pull_request_collection_is_fully_paginated():
    requests: list[dict] = []

    class Client:
        async def get(self, _endpoint, *, params):
            requests.append(params)
            page = params["page"]
            count = PAGE_SIZE if page == 1 else 1
            offset = 0 if page == 1 else PAGE_SIZE
            payload = [
                {
                    "number": offset + index + 1,
                    "title": f"PR {offset + index + 1}",
                    "html_url": (
                        f"https://github.com/acme/demo/pull/{offset + index + 1}"
                    ),
                    "closed_at": "2026-07-10T01:00:00Z",
                    "merged_at": None,
                    "updated_at": "2026-07-10T02:00:00Z",
                }
                for index in range(count)
            ]
            return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)

    pull_requests = await _manager(Client())._closed_pull_requests("acme/demo", START)

    assert len(pull_requests) == PAGE_SIZE + 1
    assert [request["page"] for request in requests] == [1, 2]
    assert all(request["per_page"] == PAGE_SIZE for request in requests)


@pytest.mark.asyncio
async def test_closed_pull_request_collection_stops_after_the_activity_window():
    requests: list[dict] = []

    class Client:
        async def get(self, _endpoint, *, params):
            requests.append(params)
            if params["page"] > 1:
                return SimpleNamespace(json=lambda: [], raise_for_status=lambda: None)
            payload = [
                {
                    "number": index + 1,
                    "title": f"PR {index + 1}",
                    "html_url": f"https://github.com/acme/demo/pull/{index + 1}",
                    "closed_at": "2026-07-10T01:00:00Z",
                    "merged_at": None,
                    "updated_at": (
                        "2026-07-10T02:00:00Z" if index < 3 else "2026-07-09T23:59:59Z"
                    ),
                }
                for index in range(PAGE_SIZE)
            ]
            return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)

    pull_requests = await _manager(Client())._closed_pull_requests("acme/demo", START)

    assert len(pull_requests) == 3
    assert [request["page"] for request in requests] == [1]
