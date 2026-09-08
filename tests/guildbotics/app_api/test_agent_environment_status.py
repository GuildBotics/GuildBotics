from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.app_api import agent_environment_status as module
from guildbotics.app_api.agent_environment_status import (
    agent_environment_problems,
    agent_environment_status,
    evaluate_grant,
)
from guildbotics.intelligences.agent_environment.contract import (
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    SharedGrants,
    parse_network_policy,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import SnapshotStatus
from guildbotics.intelligences.agent_environment.status import (
    DeviceStatus,
    DnsStatus,
    ToolStatus,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    DnsSettings,
    ToolchainDeclaration,
)
from guildbotics.intelligences.brains.cli_agent import ExecutableInfo
from guildbotics.intelligences.cli_agents import CLI_AGENTS


def _network(mode: str = "deny", **extra):
    domains = ["example.com"] if mode == "allowlist" else []
    return parse_network_policy(
        {"mode": mode, "allowed_domains": domains, **extra}, where="test"
    )


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def _device(
    monkeypatch,
    *,
    runtime_ok: bool = True,
    snapshot_state: str = "ready",
    logged_in: frozenset[str] = frozenset({"codex", "claude"}),
) -> None:
    """Stand in for the device: its runtime, snapshot, DNS, and logins."""
    health = AgentEnvironmentHealth(
        runtime_ok, "" if runtime_ok else "no hypervisor", "0.6.17"
    )
    declaration = ToolchainDeclaration(dns=DnsSettings(nameservers="host"))
    state = SnapshotStatus(
        snapshot_state,
        "guildbotics-abc",
        Path("/snap"),
        "boom" if snapshot_state == "failed" else "",
    )
    tools = tuple(
        ToolStatus(
            name=agent.name,
            label=agent.label,
            provisioned=bool(agent.provision.package),
            logged_in=agent.name in logged_in,
        )
        for agent in CLI_AGENTS
    )

    def fake(*, building_here: bool = False) -> DeviceStatus:
        return DeviceStatus(
            runtime=health,
            declaration=declaration,
            declaration_problem="",
            snapshot=state,
            dns=DnsStatus("host", ("192.168.3.1",)),
            tools=tools,
        )

    monkeypatch.setattr(module, "device_status", fake)


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module, "load_shared_grants", lambda: SharedGrants())
    monkeypatch.setattr(module, "load_local_grants", lambda: LocalGrants())
    _device(monkeypatch)
    return home


def _codex_slot(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "get_cli_agent_mapping",
        lambda _person: {
            "default": ExecutableInfo(adapter="codex", network=_network())
        },
    )


def test_status_reports_the_device_in_the_words_a_turn_is_refused_with(
    monkeypatch, home: Path
) -> None:
    _codex_slot(monkeypatch)
    _device(monkeypatch, snapshot_state="failed", logged_in=frozenset())

    status = agent_environment_status(
        ["aiko"], platform="darwin", build_output=["[apt]", "E: boom"]
    )

    assert (status.runtime.available, status.runtime.version) == (True, "0.6.17")
    assert (status.snapshot.state, status.snapshot.name, status.snapshot.detail) == (
        "failed",
        "guildbotics-abc",
        "boom",
    )
    assert status.snapshot.output == ["[apt]", "E: boom"]
    assert (status.dns.declared, status.dns.nameservers) == ("host", ["192.168.3.1"])
    assert status.problem == (
        "The agent environment failed to build on this device: boom"
    )
    tools = {tool.name: tool for tool in status.tools}
    assert tools["codex"].config_reference == "cli_agents/codex/default.yml"
    assert (tools["codex"].provisioned, tools["codex"].logged_in) == (True, False)
    assert "not logged in on this device" in tools["codex"].problem
    assert (tools["grok"].provisioned, tools["grok"].problem) == (
        False,
        "Grok Build is not provisioned in the agent environment yet.",
    )
    assert tools["claude"].problem.startswith("Claude Code is not logged in")


def test_problems_name_the_device_once_and_only_the_tools_in_use(
    monkeypatch, home: Path
) -> None:
    _codex_slot(monkeypatch)
    _device(monkeypatch, runtime_ok=False, logged_in=frozenset())

    problems = agent_environment_problems(["aiko", "kenji"])

    # The device is one problem however many members there are; Claude is
    # not logged in either, but no slot uses it, so it is not one.
    assert problems == [
        ("", "", "environment", "no hypervisor"),
        (
            "",
            "codex",
            "tool",
            "Codex is not logged in on this device; run "
            "`guildbotics environment login codex`.",
        ),
    ]


def test_a_ready_device_with_logins_has_no_problems(monkeypatch, home: Path) -> None:
    _codex_slot(monkeypatch)

    assert agent_environment_problems(["aiko"]) == []


def test_status_resolves_the_grants_once_and_names_what_each_slot_cannot_get(
    monkeypatch, home: Path
) -> None:
    (home / "tools").mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setattr(
        module,
        "load_shared_grants",
        lambda: SharedGrants(
            documents=[
                DocumentGrant(path="tools", access="read"),
                DocumentGrant(path="Projects/out", access="read_write"),
            ],
        ),
    )
    monkeypatch.setattr(
        module, "load_local_grants", lambda: LocalGrants(deny=[".local/share/x"])
    )
    mappings = {
        "aiko": {
            "default": ExecutableInfo(
                adapter="codex", network=_network("deny", allow_local_network=True)
            )
        },
        "kenji": {"default": ExecutableInfo(adapter="grok", network=_network())},
    }
    monkeypatch.setattr(
        module, "get_cli_agent_mapping", lambda person: mappings[person]
    )

    status = agent_environment_status(["aiko", "kenji"], platform="darwin")

    # A preview creates nothing: the missing document directory is shown absent.
    # Each row carries the spelling the grant file uses beside its display
    # form, so the editor matches rows to entries without re-deriving it.
    assert [(g.path, g.grant, g.present) for g in status.access.documents] == [
        ("$HOME/tools", "tools", True),
        ("$HOME/Projects/out", "Projects/out", False),
    ]
    assert not (home / "Projects/out").exists()
    assert [(d.path, d.builtin) for d in status.access.denied] == [
        ("$HOME/.ssh", True),
        ("$HOME/.local/share/x", False),
    ]
    # The environment enforces every network setting the same way, so no
    # slot is refused for what its tool could not do natively.
    aiko, kenji = status.members[0].slots[0], status.members[1].slots[0]
    assert (aiko.tool, aiko.network.allow_local_network) == ("codex", True)
    assert aiko.problems == [] and kenji.problems == []
    # What does keep kenji's slot from starting is its tool, which is not in
    # the environment yet; that is reported once for the tool, not per slot.
    assert agent_environment_problems(["aiko", "kenji"]) == [
        (
            "",
            "grok",
            "tool",
            "Grok Build is not provisioned in the agent environment yet.",
        )
    ]


def test_an_unresolvable_local_path_is_reported_on_every_slot(
    monkeypatch, home: Path
) -> None:
    monkeypatch.setattr(
        module,
        "load_local_grants",
        lambda: LocalGrants(paths=[LocalPathGrant(path="/opt/nowhere", access="read")]),
    )
    monkeypatch.setattr(
        module,
        "get_cli_agent_mapping",
        lambda _person: {
            "default": ExecutableInfo(adapter="codex", network=_network())
        },
    )

    status = agent_environment_status(["aiko"], platform="darwin")

    # The row is shown absent, and every applied slot names it as the reason.
    assert status.access.problem == ""
    assert [(g.path, g.present) for g in status.access.paths] == [
        ("/opt/nowhere", False)
    ]
    assert [(p.setting, p.reason) for p in status.members[0].slots[0].problems] == [
        ("grants", "local path '/opt/nowhere' does not exist on this device")
    ]


@pytest.mark.parametrize(
    ("scope", "path", "access", "valid", "present", "reason_part", "sensitive"),
    [
        ("document", "Documents", "read", True, True, "", ""),
        ("document", "Projects/new", "read_write", True, False, "", ""),
        ("document", "/opt/x", "read", False, False, "relative to the home", ""),
        ("document", "..", "read", False, False, "must name a directory", ""),
        ("document", ".ssh", "read", True, True, "", "~/.ssh"),
        ("local", "/opt/nowhere", "read", False, False, "does not exist", ""),
        ("local", ".cache/uv", "read_write", True, True, "", ""),
        ("local", ".codex", "read", True, True, "", "~/.codex"),
        ("deny", "/opt/homebrew/etc", "", True, True, "", ""),
        ("deny", ".local/share/some-app", "", True, True, "", ""),
        ("deny", "..", "", False, False, "must name a directory", ""),
    ],
)
def test_a_typed_grant_is_judged_before_it_is_saved(
    home: Path,
    scope,
    path: str,
    access: str,
    valid: bool,
    present: bool,
    reason_part: str,
    sensitive: str,
) -> None:
    for name in ("Documents", ".ssh", ".cache/uv", ".codex"):
        (home / name).mkdir(parents=True)

    evaluation = evaluate_grant(scope, path, access or "read")

    assert evaluation.valid is valid
    assert evaluation.present is present
    assert reason_part in evaluation.reason
    assert evaluation.sensitive == sensitive
    assert not (home / "Projects/new").exists()


def test_a_deny_that_would_close_the_home_is_refused(home: Path) -> None:
    refused = evaluate_grant("deny", str(home))

    assert refused.valid is False
    assert "whole home directory" in refused.reason


def test_the_sandbox_endpoints_answer_from_this_device(
    monkeypatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from guildbotics.app_api import api as api_module
    from guildbotics.app_api.api import create_app
    from guildbotics.app_api.events import EventBus
    from guildbotics.app_api.models import (
        AgentEnvironmentStatusResponse,
        EnvironmentRuntimeStatus,
        EnvironmentSnapshotStatus,
        GrantEvaluation,
    )
    from guildbotics.app_api.runtime import AppRuntime

    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(
        runtime,
        "get_agent_environment_status",
        lambda: AgentEnvironmentStatusResponse(
            platform="darwin",
            runtime=EnvironmentRuntimeStatus(available=True),
            snapshot=EnvironmentSnapshotStatus(state="ready"),
        ),
    )
    monkeypatch.setattr(
        api_module,
        "evaluate_grant",
        lambda scope, path, access="read": GrantEvaluation(
            scope=scope, path=path, access=access, valid=True
        ),
    )
    headers = {"X-GuildBotics-Session-Token": "secret"}

    with TestClient(create_app(session_token="secret", runtime=runtime)) as client:
        status = client.get("/intelligences/agent-environment", headers=headers)
        evaluation = client.get(
            "/intelligences/grant-evaluation",
            params={"scope": "deny", "path": "/opt/x"},
            headers=headers,
        )
        bad_scope = client.get(
            "/intelligences/grant-evaluation",
            params={"scope": "other", "path": "x"},
            headers=headers,
        )
        unauthorized = client.get("/intelligences/agent-environment")

    assert status.json()["platform"] == "darwin"
    assert evaluation.json()["scope"] == "deny"
    assert bad_scope.status_code == 422
    assert unauthorized.status_code == 401
