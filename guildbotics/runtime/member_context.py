from __future__ import annotations

from collections.abc import Sequence

from guildbotics.commands.errors import (
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.editions import get_edition
from guildbotics.entities.team import Person, Team
from guildbotics.runtime.context import Context


def resolve_member_context(person_identifier: str) -> tuple[Context, Person]:
    """Resolve a GuildBotics context and explicit member by id or name."""
    base_context = get_edition().get_context()
    person = ensure_execution_subject(
        resolve_person(base_context.team, person_identifier)
    )
    return base_context.clone_for(person), person


def ensure_execution_subject(person: Person) -> Person:
    """Return the member, rejecting the ones that are not AI execution subjects.

    A human member is a reference to a real person in the team, so any path that
    would run commands or member capabilities as that member is a boundary bug.
    """
    if person.person_type == "human":
        raise PersonExecutionNotAllowedError(person.person_id)
    return person


def resolve_person(
    team: Team,
    identifier: str | None,
    *,
    allow_default: bool = False,
) -> Person:
    """Resolve the member a command runs as.

    An omitted identifier falls back to the team default (see
    :meth:`Team.get_default_person_id`) when ``allow_default`` is set. The
    default is then treated exactly as if the caller had named that member, so
    a stale or unusable default surfaces the same error an explicit one would.
    """
    if identifier is None:
        identifier = team.get_default_person_id() if allow_default else ""
        if not identifier:
            raise PersonSelectionRequiredError(list_person_labels(team.members))

    person = find_person(team.members, identifier)
    if person is None:
        available = list_person_labels(team.members)
        raise PersonNotFoundError(identifier, available)
    return person


def find_person(members: Sequence[Person], identifier: str) -> Person | None:
    lower_identifier = identifier.casefold()
    for member in members:
        if member.person_id.casefold() == lower_identifier:
            return member
    for member in members:
        if member.name.casefold() == lower_identifier:
            return member
    return None


def list_person_labels(members: Sequence[Person]) -> list[str]:
    labels: list[str] = []
    for member in members:
        label = member.person_id
        if member.name and member.name.casefold() != member.person_id.casefold():
            label = f"{label} ({member.name})"
        labels.append(label)
    return sorted(labels)
