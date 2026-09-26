from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.app_api import runtime as runtime_module
from guildbotics.app_api import verify as verify_module
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import SnapshotStatus
from guildbotics.intelligences.agent_environment.status import (
    DeviceStatus,
    DnsStatus,
    ToolStatus,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS


@pytest.fixture(autouse=True)
def isolate_machine_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep App API service locks out of the developer's real home directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


class AgentEnvironmentStandIn:
    """This device's agent environment: ready, every tool logged in, until a
    test says otherwise."""

    def __init__(self) -> None:
        self.health = AgentEnvironmentHealth(True, "", "0.6.17")
        self.logged_out: set[str] = set()

    def refuse(self, reason: str) -> None:
        """Make the device refuse every turn with ``reason``."""
        self.health = AgentEnvironmentHealth(False, reason)

    def log_out(self, tool: str) -> None:
        self.logged_out.add(tool)

    def __call__(self, *, building_here: bool = False) -> DeviceStatus:
        del building_here
        return DeviceStatus(
            runtime=self.health,
            declaration=None,
            declaration_problem="",
            snapshot=SnapshotStatus("ready", "guildbotics-test", Path("/snap")),
            network=None,
            dns=DnsStatus(""),
            tools=tuple(
                ToolStatus(
                    name=agent.name,
                    label=agent.label,
                    provisioned=agent.provision.provisioned,
                    credentials=(
                        "missing" if agent.name in self.logged_out else "saved"
                    ),
                )
                for agent in CLI_AGENTS
            ),
        )


@pytest.fixture(autouse=True)
def agent_environment(monkeypatch: pytest.MonkeyPatch) -> AgentEnvironmentStandIn:
    """Stand in for this device's agent environment.

    Reading the real one places the bundled runtime under the test's home and
    asks the keychain for every tool's login.
    """
    device = AgentEnvironmentStandIn()
    for module in (runtime_module, verify_module):
        monkeypatch.setattr(module, "device_status", device)
    return device
