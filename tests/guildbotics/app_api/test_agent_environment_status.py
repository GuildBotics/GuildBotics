from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.app_api import agent_environment_status as module
from guildbotics.app_api.agent_environment_status import (
    agent_environment_problems,
    agent_environment_status,
    evaluate_grant,
)
from guildbotics.intelligences.agent_environment import status as device_module
from guildbotics.intelligences.agent_environment.contract import (
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    SharedGrants,
    parse_network_policy,
    resolve_access,
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
from guildbotics.utils.i18n_tool import t


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
    credentials_saved: frozenset[str] = frozenset({"codex", "claude"}),
    filesystem_problem: str = "",
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
            provisioned=agent.provision.provisioned,
            credentials_saved=agent.name in credentials_saved,
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
            filesystem_problem=filesystem_problem,
            access=resolve_access(
                device_module.load_shared_grants(),
                device_module.load_local_grants(),
                create=False,
            ),
        )

    monkeypatch.setattr(module, "device_status", fake)


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(device_module, "load_shared_grants", lambda: SharedGrants())
    monkeypatch.setattr(device_module, "load_local_grants", lambda: LocalGrants())
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
    _device(monkeypatch, snapshot_state="failed", credentials_saved=frozenset())

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
    assert (status.problem, status.problem_setting) == (
        t("intelligences.agent_environment.snapshot.failed", detail="boom"),
        "snapshot",
    )
    tools = {tool.name: tool for tool in status.tools}
    assert tools["codex"].config_reference == "cli_agents/codex/default.yml"
    assert (tools["codex"].provisioned, tools["codex"].credentials_saved) == (
        True,
        False,
    )
    assert tools["codex"].problem == t(
        "intelligences.agent_environment.tool.credentials_missing",
        tool="Codex",
        name="codex",
    )
    assert (tools["grok"].provisioned, tools["grok"].problem) == (
        True,
        t(
            "intelligences.agent_environment.tool.credentials_missing",
            tool="Grok Build",
            name="grok",
        ),
    )
    assert tools["claude"].problem == t(
        "intelligences.agent_environment.tool.credentials_missing",
        tool="Claude Code",
        name="claude",
    )


def test_problems_name_the_device_once_and_only_the_tools_in_use(
    monkeypatch, home: Path
) -> None:
    _codex_slot(monkeypatch)
    _device(monkeypatch, runtime_ok=False, credentials_saved=frozenset())

    problems = agent_environment_problems(["aiko", "kenji"])

    # The device is one problem however many members there are, named by the
    # part that is wrong; Claude is not logged in either, but no slot uses it,
    # so it is not one.
    assert problems == [
        ("", "", "runtime", "no hypervisor"),
        (
            "",
            "codex",
            "tool",
            t(
                "intelligences.agent_environment.tool.credentials_missing",
                tool="Codex",
                name="codex",
            ),
        ),
    ]


def test_a_ready_device_with_logins_has_no_problems(monkeypatch, home: Path) -> None:
    _codex_slot(monkeypatch)

    assert agent_environment_problems(["aiko"]) == []


def test_filesystem_refusal_is_shared_by_status_and_alerts(monkeypatch, home):
    _codex_slot(monkeypatch)
    reason = t("intelligences.agent_environment.filesystem.macos_documents", app="")
    _device(monkeypatch, filesystem_problem=reason)
    status = agent_environment_status(["aiko"])
    assert (status.problem, status.problem_setting) == (reason, "filesystem")
    assert ("", "", "filesystem", reason) in agent_environment_problems(["aiko"])


def test_status_resolves_the_grants_once_and_names_what_each_slot_cannot_get(
    monkeypatch, home: Path
) -> None:
    (home / "tools").mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setattr(
        device_module,
        "load_shared_grants",
        lambda: SharedGrants(
            documents=[
                DocumentGrant(path="tools", access="read"),
                DocumentGrant(path="Projects/out", access="read_write"),
            ],
        ),
    )
    monkeypatch.setattr(
        device_module, "load_local_grants", lambda: LocalGrants(deny=[".local/share/x"])
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
    # The exchange directory leads, granted by GuildBotics itself.
    assert [
        (g.path, g.grant, g.present, g.builtin) for g in status.access.documents
    ] == [
        ("$HOME/Documents/GuildBotics", "Documents/GuildBotics", False, True),
        ("$HOME/tools", "tools", True, False),
        ("$HOME/Projects/out", "Projects/out", False, False),
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
    # What does keep kenji's slot from starting is its tool, which is not
    # logged in here; that is reported once for the tool, not per slot.
    assert agent_environment_problems(["aiko", "kenji"]) == [
        (
            "",
            "grok",
            "tool",
            t(
                "intelligences.agent_environment.tool.credentials_missing",
                tool="Grok Build",
                name="grok",
            ),
        )
    ]


def test_an_unresolvable_local_path_is_reported_on_every_slot(
    monkeypatch, home: Path
) -> None:
    monkeypatch.setattr(
        device_module,
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
        (
            "grants",
            t(
                "intelligences.agent_environment.grants.local_path_missing",
                path="/opt/nowhere",
            ),
        )
    ]


@pytest.mark.parametrize(
    ("scope", "path", "access", "valid", "present", "reason", "sensitive"),
    [
        ("document", "Documents", "read", True, True, "", ""),
        ("document", "Projects/new", "read_write", True, False, "", ""),
        ("document", "/opt/x", "read", False, False, "path_not_relative", ""),
        ("document", "..", "read", False, False, "path_not_a_directory_name", ""),
        ("document", ".ssh", "read", True, True, "", "~/.ssh"),
        ("local", "/opt/nowhere", "read", False, False, "local_path_missing", ""),
        ("local", ".cache/uv", "read_write", True, True, "", ""),
        ("local", ".codex", "read", True, True, "", "~/.codex"),
        ("deny", "/opt/homebrew/etc", "", True, True, "", ""),
        ("deny", ".local/share/some-app", "", True, True, "", ""),
        ("deny", "..", "", False, False, "path_not_a_directory_name", ""),
    ],
)
def test_a_typed_grant_is_judged_before_it_is_saved(
    home: Path,
    scope,
    path: str,
    access: str,
    valid: bool,
    present: bool,
    reason: str,
    sensitive: str,
) -> None:
    for name in ("Documents", ".ssh", ".cache/uv", ".codex"):
        (home / name).mkdir(parents=True)

    evaluation = evaluate_grant(scope, path, access or "read")

    assert evaluation.valid is valid
    assert evaluation.present is present
    # ``reason`` names the sentence under intelligences.agent_environment.grants.
    expected = t(f"intelligences.agent_environment.grants.{reason}", path=path)
    assert evaluation.reason == (expected if reason else "")
    assert evaluation.sensitive == sensitive
    assert not (home / "Projects/new").exists()


def test_a_deny_that_would_close_the_home_is_refused(home: Path) -> None:
    refused = evaluate_grant("deny", str(home))

    assert refused.valid is False
    assert refused.reason == t(
        "intelligences.agent_environment.grants.deny_too_broad", path=str(home)
    )


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


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_login_instructions_use_desktop_managed_cli(monkeypatch, home, platform):
    import shlex

    special_home = home / "A user's home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: special_home))
    for tool in agent_environment_status([], platform=platform).tools:
        if platform == "win32":
            expected = str(special_home / ".guildbotics/bin/guildbotics.exe").replace(
                "'", "''"
            )
            assert tool.login_command == f"& '{expected}' environment login {tool.name}"
        else:
            assert shlex.split(tool.login_command) == [
                str(special_home / ".guildbotics/bin/guildbotics"),
                "environment",
                "login",
                tool.name,
            ]


def test_device_authentication_failure_drives_card_and_alert_then_recovers(
    monkeypatch, home
):
    from guildbotics.app_api.models import RuntimeStatus, RuntimeUnitStatus
    from guildbotics.app_api.system_alerts import SystemAlertService
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.cli_agents import cli_agent_info

    _codex_slot(monkeypatch)
    original = module.device_status()
    tool = cli_agent_info("codex")
    store = provider_state.provider_state_dir(tool)
    store.mkdir(parents=True)
    (store / tool.provision.auth).write_text("{}")
    from dataclasses import replace

    monkeypatch.setattr(
        module,
        "device_status",
        lambda **_: replace(
            original,
            tools=tuple(device_module._tool_status(agent) for agent in CLI_AGENTS),
        ),
    )
    service = SystemAlertService(None)
    persons = ["alice", "bob"]
    for failed in (True, False, True, False):
        provider_state.record_authentication_outcome(tool, failed=failed)
        status = agent_environment_status(persons)
        codex = next(item for item in status.tools if item.name == "codex")
        assert codex.authentication_failed is failed
        assert codex.credentials_saved
        assert {member.slots[0].tool for member in status.members} == {"codex"}
        problems = agent_environment_problems(persons)
        alerts = service.list_alerts(
            RuntimeStatus(
                scheduler=RuntimeUnitStatus(
                    target="scheduler", state="stopped", running=False
                ),
                events=RuntimeUnitStatus(
                    target="events", state="stopped", running=False
                ),
            ),
            problems,
        ).alerts
        if failed:
            reason = t(
                "intelligences.agent_environment.tool.authentication_failed",
                tool="Codex",
                name="codex",
            )
            assert codex.problem == reason
            assert problems == [("", "codex", "tool", reason)]
            assert [(a.person_id, a.command, a.reason) for a in alerts] == [
                ("", "codex", reason)
            ]
        else:
            assert codex.problem == ""
            assert problems == []
            assert alerts == []
