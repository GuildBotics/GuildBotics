"""The team a test's workspace holds, for the contexts the test builds.

A :class:`~guildbotics.runtime.context.Context` reads the team from the
workspace's configuration, anew for every context. With the
``configured_team`` fixture on, it reads the team :func:`make_context` names.
"""

from __future__ import annotations

from guildbotics.entities.team import Person, Team
from guildbotics.runtime.brain_factory import BrainFactory
from guildbotics.runtime.context import Context
from guildbotics.runtime.integration_factory import IntegrationFactory


class ConfiguredTeam:
    """Stands in for the team the workspace's configuration holds, which
    every :class:`Context` reads when the ``configured_team`` fixture is on."""

    def __init__(self) -> None:
        self.team: Team | None = None
        self.loads = 0

    def load(self) -> Team:
        assert self.team is not None, "make_context names the team"
        self.loads += 1
        return self.team


CONFIGURED_TEAM = ConfiguredTeam()


def make_context(
    team: Team,
    integration_factory: IntegrationFactory,
    brain_factory: BrainFactory,
    message: str = "",
    person: Person | None = None,
) -> Context:
    """A context whose workspace holds ``team`` (needs ``configured_team``)."""
    CONFIGURED_TEAM.team = team
    return Context(integration_factory, brain_factory, person, message)
