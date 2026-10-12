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
from tests.guildbotics.local_code_host import pull_request, ticket

START = datetime(2026, 7, 10, tzinfo=UTC)
END = datetime(2026, 7, 11, tzinfo=UTC)


def _team(board: str = "local") -> Team:
    return Team(
        project=Project(services={"ticket_service": {"name": board}}),
        members=[Person(person_id="aiko", name="Aiko")],
    )


def _closed(closed_at: str) -> dict:
    return {"state": "closed", "closed_at": closed_at}


@pytest.mark.asyncio
async def test_closed_board_work_is_recorded_once_with_its_activity():
    merged_url = pull_request(
        7, "feature", merged=True, **_closed("2026-07-10T01:00:00+00:00")
    )
    closed_url = ticket(8, **_closed("2026-07-10T02:00:00+00:00"))
    ticket(9, **_closed("2026-07-12T00:00:00+00:00"))

    assert await refresh_activity_events(_team(), START, END) == 2
    assert await refresh_activity_events(_team(), START, END) == 0

    records = ActivityEventStore().records_between(START, END)
    assert sorted(
        (record["timestamp"], record["attributes"]["github.activity_id"])
        for record in records
    ) == [
        ("2026-07-10T01:00:00+00:00", f"pull_request:{merged_url}:merged"),
        ("2026-07-10T02:00:00+00:00", f"ticket:{closed_url}:closed"),
    ]
    merged = next(r for r in records if r["attributes"]["github.label"] == "!7")
    assert merged["type"] == "github.pull_request"
    assert merged["payload"]["pull_request"] == {
        "label": "!7",
        "title": "Pull request 7",
        "url": merged_url,
        "merged": True,
    }
    assert merged["attributes"]["github.url"] == merged_url
    assert merged["person_id"] == ""
    closed = next(r for r in records if r["attributes"]["github.label"] == "T-8")
    assert closed["type"] == "github.issue"
    assert closed["attributes"]["github.kind"] == "ticket"


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
                "ticket_service": {
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
    assert record["attributes"]["github.activity_id"] == (
        f"ticket:https://github.com/{REPO}/issues/1:closed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("board", ["", "unsupported"])
async def test_without_a_board_nothing_is_read(board):
    assert await refresh_activity_events(_team(board), START, END) == 0


def test_existing_activity_ids_reads_shared_activity_events():
    activity_id = "ticket:local://tickets/acme/team/demo-tickets/T-8:closed"
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
            "attributes": {
                "github.activity_id": "ticket:local://tickets/acme/team/demo-tickets/T-9:closed"
            },
        }
    )

    existing = _existing_activity_ids(START, END)

    assert activity_id in existing
    # Out-of-range events do not count as existing.
    assert "ticket:local://tickets/acme/team/demo-tickets/T-9:closed" not in existing
