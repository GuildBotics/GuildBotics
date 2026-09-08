"""One reading of the device answers the CLI, the Desktop, and a refused turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.intelligences.agent_environment import status as module
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import SnapshotStatus
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_environment.toolchain import (
    DnsSettings,
    ToolchainDeclaration,
    ToolchainError,
)


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A device whose parts a test can swap one at a time."""
    parts: dict[str, object] = {
        "health": AgentEnvironmentHealth(True, "", "0.6.17"),
        "declaration": ToolchainDeclaration(dns=DnsSettings(nameservers="host")),
        "snapshot": SnapshotStatus("ready", "guildbotics-abc", Path("/snap")),
        "nameservers": ("192.168.3.1",),
        "logged_in": {"codex"},
    }

    def load() -> ToolchainDeclaration:
        declaration = parts["declaration"]
        if isinstance(declaration, ToolchainError):
            raise declaration
        assert isinstance(declaration, ToolchainDeclaration)
        return declaration

    def resolvers(dns: DnsSettings) -> tuple[str, ...]:
        nameservers = parts["nameservers"]
        if isinstance(nameservers, ToolchainError):
            raise nameservers
        assert isinstance(nameservers, tuple)
        return nameservers

    monkeypatch.setattr(module.runtime, "doctor", lambda: parts["health"])
    monkeypatch.setattr(module, "load_toolchain", load)
    monkeypatch.setattr(module.snapshot, "snapshot_status", lambda d: parts["snapshot"])
    monkeypatch.setattr(module, "upstream_nameservers", resolvers)
    monkeypatch.setattr(
        module, "is_logged_in", lambda agent: agent.name in parts["logged_in"]
    )
    return parts


def test_a_ready_device_refuses_nothing_but_a_missing_login(device) -> None:
    status = device_status()

    assert status.ready and status.refusal == ""
    assert (status.dns.declared, status.dns.nameservers) == ("host", ("192.168.3.1",))
    assert status.tool("codex").refusal == ""
    assert status.tool("claude").refusal == (
        "Claude Code is not logged in on this device; run "
        "`guildbotics environment login claude`."
    )
    assert status.tool("grok").refusal == (
        "Grok Build is not provisioned in the agent environment yet."
    )
    with pytest.raises(ValueError):
        status.tool("nope")


@pytest.mark.parametrize(
    ("part", "value", "expected"),
    [
        ("health", AgentEnvironmentHealth(False, "no hypervisor"), "no hypervisor"),
        (
            "declaration",
            ToolchainError("agent_environment.yml: bad"),
            "agent_environment.yml: bad",
        ),
        ("nameservers", ToolchainError("no IPv4 resolver"), "no IPv4 resolver"),
        (
            "snapshot",
            SnapshotStatus("missing", "guildbotics-abc", Path("/snap")),
            "The agent environment is missing on this device; build it with "
            "`guildbotics environment build`.",
        ),
        (
            "snapshot",
            SnapshotStatus("stale", "guildbotics-abc", Path("/snap")),
            "The agent environment is stale on this device; build it with "
            "`guildbotics environment build`.",
        ),
        (
            "snapshot",
            SnapshotStatus("building", "guildbotics-abc", Path("/snap")),
            "The agent environment is being built on this device; try again when "
            "it is ready.",
        ),
        (
            "snapshot",
            SnapshotStatus("failed", "guildbotics-abc", Path("/snap"), "E: boom"),
            "The agent environment failed to build on this device: E: boom",
        ),
    ],
)
def test_the_refusal_is_the_first_thing_a_turn_would_stop_on(
    device, part: str, value: object, expected: str
) -> None:
    device[part] = value

    status = device_status()

    assert status.refusal == expected
    assert not status.ready


def test_an_unreadable_declaration_leaves_no_snapshot_or_dns_to_report(device) -> None:
    device["declaration"] = ToolchainError("agent_environment.yml: bad")

    status = device_status()

    assert status.snapshot is None
    assert status.declaration_problem == "agent_environment.yml: bad"
    assert status.dns == module.DnsStatus(declared="")


def test_a_build_this_process_just_started_reads_as_building(device) -> None:
    device["snapshot"] = SnapshotStatus("missing", "guildbotics-abc", Path("/snap"))

    assert device_status(building_here=True).snapshot is not None
    assert device_status(building_here=True).snapshot.state == "building"  # type: ignore[union-attr]
    # A snapshot that is already there is not un-built by a stray build.
    device["snapshot"] = SnapshotStatus("ready", "guildbotics-abc", Path("/snap"))
    assert device_status(building_here=True).snapshot.state == "ready"  # type: ignore[union-attr]
