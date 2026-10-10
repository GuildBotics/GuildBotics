"""The board's closed work becomes shared activity, once each."""

from datetime import UTC, datetime

import pytest

from guildbotics.capabilities.activity_events import (
    _existing_activity_ids,
    refresh_activity_events,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.activity_event_store import ActivityEventStore
from tests.guildbotics.integrations.contracts.providers import (
    MEMBER,
    OWNER,
    REPO,
    GitHubDouble,
    serve,
)
from tests.guildbotics.local_code_host import write

START = datetime(2026, 7, 10, tzinfo=UTC)
END = datetime(2026, 7, 11, tzinfo=UTC)


def _team(board: str = "local") -> Team:
    return Team(
        project=Project(services={"ticket_manager": {"name": board}}),
        members=[Person(person_id="aiko", name="Aiko")],
    )


def _closed(number: int, kind: str, closed_at: str, merged: bool = False) -> None:
    write(
        "acme",
        "demo",
        {
            "kind": kind,
            "number": number,
            "title": f"Item {number}",
            "body": "",
            "state": "closed",
            "author": "aiko",
            "created_at": "2026-07-01T00:00:00+00:00",
            "closed_at": closed_at,
            "merged": merged,
        },
    )


@pytest.mark.asyncio
async def test_closed_board_work_is_recorded_once_with_its_activity():
    _closed(7, "pull_request", "2026-07-10T01:00:00+00:00", merged=True)
    _closed(8, "issue", "2026-07-10T02:00:00+00:00")
    _closed(9, "issue", "2026-07-12T00:00:00+00:00")

    assert await refresh_activity_events(_team(), START, END) == 2
    assert await refresh_activity_events(_team(), START, END) == 0

    records = ActivityEventStore().records_between(START, END)
    assert sorted(
        (record["timestamp"], record["attributes"]["github.activity_id"])
        for record in records
    ) == [
        ("2026-07-10T01:00:00+00:00", "pull_request:acme/demo:7:merged"),
        ("2026-07-10T02:00:00+00:00", "issue:acme/demo:8:closed"),
    ]
    merged = next(r for r in records if r["attributes"]["github.number"] == "7")
    assert merged["payload"]["pull_request"]["merged"] is True
    assert merged["attributes"]["github.url"] == "local://acme/demo/pull/7"
    assert merged["person_id"] == ""


@pytest.mark.asyncio
async def test_the_github_board_is_read_with_a_credential_alone(monkeypatch):
    """Listing closed work decides nobody's work, so a member's credential is
    enough to read it: the member needs no ``github_username``."""
    double = GitHubDouble()
    serve(double, monkeypatch)
    double._new("issue", {"title": "Done"}).update(
        state="closed", closed_at="2026-07-10T01:00:00Z"
    )
    double.board[1] = "Done"
    team = Team(
        project=Project(
            services={
                "ticket_manager": {
                    "name": "github",
                    "owner": OWNER,
                    "project_id": "1",
                    "url": f"https://github.com/orgs/{OWNER}/projects/1",
                }
            }
        ),
        members=[
            Person(person_id="nobody", name="Nobody"),
            Person(person_id=MEMBER, name="Aiko", person_type="agent"),
        ],
    )

    assert await refresh_activity_events(team, START, END) == 1
    [record] = ActivityEventStore().records_between(START, END)
    assert record["attributes"]["github.activity_id"] == f"issue:{REPO}:1:closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("board", ["", "unsupported"])
async def test_without_a_board_nothing_is_read(board):
    assert await refresh_activity_events(_team(board), START, END) == 0


def test_existing_activity_ids_reads_shared_activity_events():
    activity_id = "issue:acme/demo:8:closed"
    events = ActivityEventStore()
    events.record(
        {
            "type": "github.issue.closed",
            "timestamp": "2026-07-10T01:00:00Z",
            "attributes": {"github.activity_id": activity_id},
        }
    )
    for index in range(20):
        events.record(
            {
                "type": "github.issue.closed",
                "timestamp": "2026-07-10T02:00:00Z",
                "attributes": {"filler": str(index)},
            }
        )
    events.record(
        {
            "type": "github.issue.closed",
            "timestamp": "2026-07-12T01:00:00Z",
            "attributes": {"github.activity_id": "issue:acme/demo:9:closed"},
        }
    )

    existing = _existing_activity_ids(START, END)

    assert activity_id in existing
    # Out-of-range events do not count as existing.
    assert "issue:acme/demo:9:closed" not in existing
