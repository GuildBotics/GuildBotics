"""Import the board's completed work as shared activity events."""

from __future__ import annotations

from datetime import datetime
from logging import getLogger

from guildbotics.capabilities.member_activity_events import (
    EVENT_SUBJECTS,
    target_record,
)
from guildbotics.entities.team import Service, Team
from guildbotics.integrations.factory import (
    PROVIDERS,
    ServiceIntegrationFactory,
)
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_events import record_correlated_event


async def refresh_activity_events(team: Team, start: datetime, end: datetime) -> int:
    """Record the board's work closed between ``start`` and ``end`` once each.

    The board is the configured scope of GuildBotics work, so this avoids
    inventing a repository list or subscribing to unrelated events. The board
    is read as the first member it would accept the credential of.
    """
    provider = PROVIDERS.get(team.project.get_service_name(Service.TICKET_SERVICE))
    person = next(
        (
            member
            for member in team.members
            if provider is not None and provider.credentialed(member)
        ),
        None,
    )
    if person is None:
        return 0
    tickets = ServiceIntegrationFactory().create_ticket_service(
        getLogger(__name__), person, team
    )
    try:
        closed = await tickets.closed_since(start, end)
    finally:
        await tickets.aclose()
    existing = _existing_activity_ids(start, end)
    recorded = 0
    for item in closed:
        activity = "merged" if item.merged else "closed"
        activity_id = f"{item.kind}:{item.url}:{activity}"
        if activity_id in existing:
            continue
        entry, attributes = target_record(item.model_dump())
        subject = EVENT_SUBJECTS[item.kind]
        record_correlated_event(
            event_type=f"github.{subject}",
            payload={"action": "closed", subject: {**entry, "merged": item.merged}},
            attributes={
                "github.action": "closed",
                **attributes,
                "github.activity_id": activity_id,
            },
            default_source="github",
            timestamp=item.closed_at,
        )
        existing.add(activity_id)
        recorded += 1
    return recorded


def _existing_activity_ids(start: datetime, end: datetime) -> set[str]:
    return {
        str(attributes.get("github.activity_id"))
        for item in ActivityEventStore().list_between(start, end)
        if isinstance((attributes := item.get("attributes")), dict)
        and attributes.get("github.activity_id")
    }
