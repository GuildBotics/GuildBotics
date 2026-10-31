from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api import command_catalog as catalog_module
from guildbotics.app_api import verify as verify_module
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.environment.runtime import AgentEnvironmentHealth
from guildbotics.environment.snapshot import SnapshotStatus
from guildbotics.environment.status import (
    DeviceStatus,
    DnsStatus,
    ToolStatus,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from tests.guildbotics.app_api.owner import owner_client


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use the temporary workspace as the runtime's working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _stop_transcript_retention(monkeypatch: pytest.MonkeyPatch):
    """Stop the retention a runtime starts on entering a workspace, which the
    app's lifespan stops in production."""
    started: list[DiagnosticsStore] = []
    start = DiagnosticsStore.start_maintenance

    def tracked(store: DiagnosticsStore) -> None:
        started.append(store)
        start(store)

    monkeypatch.setattr(DiagnosticsStore, "start_maintenance", tracked)
    yield
    for store in started:
        store.stop_maintenance()


@pytest.fixture
def client(workspace: Path) -> TestClient:
    """Bind a client to a real runtime rooted at the temporary workspace."""
    return owner_client(create_app(runtime=AppRuntime(EventBus())))


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
    for module in (catalog_module, verify_module):
        monkeypatch.setattr(module, "device_status", device)
    return device
