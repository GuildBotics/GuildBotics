from __future__ import annotations

from collections.abc import Callable
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


@pytest.fixture(autouse=True)
def agent_environment(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Stand in for this device's agent environment, ready unless told why not.

    Reading the real one places the bundled runtime under the test's home and
    asks the keychain for every tool's login. Returns a function that makes
    the device refuse every turn with the given reason.
    """
    health = [AgentEnvironmentHealth(True, "", "0.6.17")]

    def device(*, building_here: bool = False) -> DeviceStatus:
        del building_here
        return DeviceStatus(
            runtime=health[0],
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
                    credentials="saved",
                )
                for agent in CLI_AGENTS
            ),
        )

    for module in (runtime_module, verify_module):
        monkeypatch.setattr(module, "device_status", device)

    def refuse(reason: str) -> None:
        health[0] = AgentEnvironmentHealth(False, reason)

    return refuse
