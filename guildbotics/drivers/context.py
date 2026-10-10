"""Where the host builds a :class:`~guildbotics.runtime.context.Context`.

Every host entry builds its context here, with the integrations
``project.services`` names and the brains the members' mappings name. The
only other place a context is built is the entry inside a command's isolated
environment (``guest/entry.py``), which reaches the integrations
through the command's window to the host instead.
"""

from __future__ import annotations

from guildbotics.drivers.member_context import ensure_execution_subject, resolve_person
from guildbotics.entities.team import Person
from guildbotics.environment.inference_host import DirectInference
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.intelligences.brains.factory import ConfiguredBrainFactory
from guildbotics.intelligences.brains.inference import install_inference
from guildbotics.runtime.context import Context


def create_context(message: str = "") -> Context:
    """The host's context for the workspace's team, before it names a member.

    Its brains call the external inference APIs themselves, with the keys the
    host holds.
    """
    install_inference(DirectInference())
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
