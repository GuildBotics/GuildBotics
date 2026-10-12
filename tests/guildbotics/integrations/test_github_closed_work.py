"""The GitHub Project's closed work: its own closed items, and the closed pull
requests of the repositories it holds items of."""

from datetime import UTC, datetime

import pytest

from guildbotics.integrations.github.ticket_service import _closed_item
from guildbotics.runtime.ticket_service import ClosedItem
from tests.guildbotics.integrations.github_services import (
    REPO,
    ScriptedGitHub,
    issue_url,
    pages,
    pull_url,
    ticket_service,
)

PAGE_SIZE = 100
START = datetime(2026, 7, 10, tzinfo=UTC)
END = datetime(2026, 7, 11, tzinfo=UTC)
_ISSUE = {
    "__typename": "Issue",
    "number": 8,
    "title": "Done",
    "url": issue_url(8),
    "state": "CLOSED",
    "closedAt": "2026-07-10T01:00:00Z",
    "repository": {"name": "demo", "owner": {"login": "acme"}},
}


def _closed_pull(number: int, updated_at: str = "2026-07-10T02:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": pull_url(number),
        "closed_at": "2026-07-10T01:00:00Z",
        "merged_at": None,
        "updated_at": updated_at,
    }


def _board(github: ScriptedGitHub, monkeypatch, contents: list[dict]):
    github.on_graphql(
        "items(first: 100",
        {
            "node": {
                "items": {
                    "nodes": [{"content": content} for content in contents],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
    )
    tickets = ticket_service(github, monkeypatch)
    tickets._project_node_id = "PROJECT"
    return tickets


def test_a_closed_item_is_named_by_its_merge_or_close():
    merged = _closed_item(
        {
            **_ISSUE,
            "__typename": "PullRequest",
            "number": 7,
            "url": pull_url(7),
            "mergedAt": "2026-07-10T02:00:00Z",
        }
    )
    issue = _closed_item(_ISSUE)

    assert merged == ClosedItem(
        kind="pull_request",
        url=pull_url(7),
        title="Done",
        label="#7",
        closed_at="2026-07-10T02:00:00Z",
        merged=True,
    )
    assert issue == ClosedItem(
        kind="ticket",
        url=issue_url(8),
        title="Done",
        label="#8",
        closed_at="2026-07-10T01:00:00Z",
        merged=False,
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
async def test_closed_since_keeps_the_window_and_drops_unreadable_times(
    monkeypatch, closed_at
):
    github = ScriptedGitHub().on("GET", f"/repos/{REPO}/pulls", [])
    tickets = _board(
        github,
        monkeypatch,
        [
            _ISSUE,
            {**_ISSUE, "number": 9, "closedAt": closed_at},
            {**_ISSUE, "number": 10, "closedAt": "2026-07-12T00:00Z"},
        ],
    )

    closed = await tickets.closed_since(START, END)

    assert [item.label for item in closed] == ["#8"]


@pytest.mark.asyncio
async def test_closed_since_adds_the_closed_pull_requests_of_the_boards_repositories(
    monkeypatch,
):
    github = ScriptedGitHub().on(
        "GET", f"/repos/{REPO}/pulls", pages([_closed_pull(3)])
    )
    tickets = _board(github, monkeypatch, [_ISSUE])

    closed = await tickets.closed_since(START, END)

    assert [(item.kind, item.url, item.label) for item in closed] == [
        ("pull_request", pull_url(3), "#3"),
        ("ticket", issue_url(8), "#8"),
    ]
    [request] = [r for r in github.requests if r.url.path.endswith("/pulls")]
    assert request.url.params["state"] == "closed"


@pytest.mark.asyncio
async def test_closed_pull_request_collection_is_fully_paginated(monkeypatch):
    github = ScriptedGitHub().on(
        "GET",
        f"/repos/{REPO}/pulls",
        pages([_closed_pull(number) for number in range(1, PAGE_SIZE + 2)]),
    )
    tickets = ticket_service(github, monkeypatch)

    pull_requests = await tickets._closed_pull_requests(REPO, START)

    assert len(pull_requests) == PAGE_SIZE + 1
    assert [
        (r.url.params["page"], r.url.params["per_page"]) for r in github.requests
    ] == [("1", "100"), ("2", "100")]


@pytest.mark.asyncio
async def test_closed_pull_request_collection_stops_after_the_activity_window(
    monkeypatch,
):
    github = ScriptedGitHub().on(
        "GET",
        f"/repos/{REPO}/pulls",
        pages(
            [
                _closed_pull(
                    number,
                    "2026-07-10T02:00:00Z" if number <= 3 else "2026-07-09T23:59:59Z",
                )
                for number in range(1, PAGE_SIZE + 1)
            ]
            + [_closed_pull(PAGE_SIZE + 1)]
        ),
    )
    tickets = ticket_service(github, monkeypatch)

    pull_requests = await tickets._closed_pull_requests(REPO, START)

    assert [item.label for item in pull_requests] == ["#1", "#2", "#3"]
    assert [r.url.params["page"] for r in github.requests] == ["1"]
