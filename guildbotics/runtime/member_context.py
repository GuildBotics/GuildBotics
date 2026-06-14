from __future__ import annotations

from collections.abc import Sequence

from guildbotics.commands.errors import (
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.editions import get_edition
from guildbotics.entities.team import Person
from guildbotics.runtime.context import Context


def resolve_member_context(person_identifier: str) -> tuple[Context, Person]:
    """Resolve a GuildBotics context and explicit member by id or name."""
    base_context = get_edition().get_context()
    person = resolve_person(base_context.team.members, person_identifier)
    return base_context.clone_for(person), person


def resolve_person(
    members: Sequence[Person],
    identifier: str | None,
    *,
    default_to_single_active: bool = False,
) -> Person:
    if identifier is None:
        if default_to_single_active:
            active_members = [member for member in members if member.is_active]
            if len(active_members) == 1:
                return active_members[0]
        available = list_person_labels(members)
        raise PersonSelectionRequiredError(available)

    person = find_person(members, identifier)
    if person is None:
        available = list_person_labels(members)
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
