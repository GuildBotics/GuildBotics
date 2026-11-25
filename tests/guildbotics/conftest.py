"""Shared fixtures for workflow presentation tests."""

import pytest


@pytest.fixture
def configured_team(monkeypatch):
    """Every context reads the team ``make_context`` names instead of the
    workspace's configuration."""
    from guildbotics.runtime import context
    from tests.guildbotics.runtime.configured_team import CONFIGURED_TEAM

    monkeypatch.setattr(context, "YamlTeamLoader", lambda: CONFIGURED_TEAM)
    yield CONFIGURED_TEAM
    CONFIGURED_TEAM.team = None
    CONFIGURED_TEAM.loads = 0
