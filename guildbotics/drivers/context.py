"""Where the host builds a :class:`~guildbotics.runtime.context.Context`.

Every host entry builds its context here, with the integrations
``project.services`` names and the brains the members' mappings name. The
only other place a context is built is the entry inside a command's isolated
environment (``runtime/command_entry.py``), which reaches the integrations
through the command's window to the host instead.
"""

from __future__ import annotations

from guildbotics.entities.team import Person
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.intelligences.brains.factory import ConfiguredBrainFactory
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_context import ensure_execution_subject, resolve_person


def create_context(message: str = "") -> Context:
    """The host's context for the workspace's team, before it names a member."""
    return Context(
        ServiceIntegrationFactory(), ConfiguredBrainFactory(), message=message
    )


def resolve_member_context(person_identifier: str) -> tuple[Context, Person]:
    """Resolve a GuildBotics context and explicit member by id or name."""
    base_context = create_context()
    person = ensure_execution_subject(
        resolve_person(base_context.team, person_identifier)
    )
    return base_context.clone_for(person), person
