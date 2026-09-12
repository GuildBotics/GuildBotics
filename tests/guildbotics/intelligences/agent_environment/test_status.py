"""One reading of the device answers the CLI, the Desktop, and a refused turn."""

from __future__ import annotations

from pathlib import Path
import errno

import pytest

from guildbotics.intelligences.agent_environment.status import login_command
from guildbotics.intelligences.agent_environment import status as module
from guildbotics.intelligences.agent_runtime.environment import _ready
from guildbotics.intelligences.agent_runtime.models import AgentRuntimeError
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import SnapshotStatus
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_environment.contract import (
    DocumentGrant,
    SharedGrants,
    LocalGrants,
    LocalPathGrant,
)
from guildbotics.intelligences.cli_agents import CliAgentInfo
from guildbotics.intelligences.agent_environment.toolchain import (
    DnsSettings,
    ToolchainDeclaration,
    ToolchainError,
)
from guildbotics.utils.i18n_tool import t


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, object]:
    """A device whose parts a test can swap one at a time."""
    parts: dict[str, object] = {
        "health": AgentEnvironmentHealth(True, "", "0.6.17"),
        "declaration": ToolchainDeclaration(dns=DnsSettings(nameservers="host")),
        "snapshot": SnapshotStatus("ready", "guildbotics-abc", Path("/snap")),
        "nameservers": ("192.168.3.1",),
        "credentials_saved": {"codex"},
    }
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))

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
        module,
        "has_credentials",
        lambda agent: agent.name in parts["credentials_saved"],
    )
    return parts


def test_a_ready_device_refuses_nothing_but_a_missing_login(device) -> None:
    status = device_status()

    assert status.ready and (status.refusal, status.setting) == ("", "")
    assert (status.dns.declared, status.dns.nameservers) == ("host", ("192.168.3.1",))
    assert status.tool("codex").refusal == ""
    assert status.tool("claude").refusal == t(
        "intelligences.agent_environment.tool.credentials_missing",
        tool="Claude Code",
        command=login_command("claude"),
    )
    assert status.tool("grok").refusal == t(
        "intelligences.agent_environment.tool.credentials_missing",
        tool="Grok Build",
        command=login_command("grok"),
    )
    with pytest.raises(ValueError):
        status.tool("nope")


def test_a_tool_the_snapshot_does_not_carry_is_refused_as_such(
    device, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = CliAgentInfo(name="ghost", label="Ghost", executable="ghost")
    monkeypatch.setattr(module, "CLI_AGENTS", (*module.CLI_AGENTS, ghost))

    status = device_status()

    assert not status.tool("ghost").provisioned
    assert status.tool("ghost").refusal == t(
        "intelligences.agent_environment.tool.not_provisioned", tool="Ghost"
    )


@pytest.mark.parametrize(
    ("part", "value", "expected", "setting"),
    [
        (
            "health",
            AgentEnvironmentHealth(False, "no hypervisor"),
            "no hypervisor",
            "runtime",
        ),
        (
            "declaration",
            ToolchainError("agent_environment.yml: bad"),
            "agent_environment.yml: bad",
            "declaration",
        ),
        (
            "nameservers",
            ToolchainError("no IPv4 resolver"),
            "no IPv4 resolver",
            "declaration",
        ),
        (
            "snapshot",
            SnapshotStatus("missing", "guildbotics-abc", Path("/snap")),
            t("intelligences.agent_environment.snapshot.missing"),
            "snapshot",
        ),
        (
            "snapshot",
            SnapshotStatus("stale", "guildbotics-abc", Path("/snap")),
            t("intelligences.agent_environment.snapshot.stale"),
            "snapshot",
        ),
        (
            "snapshot",
            SnapshotStatus("building", "guildbotics-abc", Path("/snap")),
            t("intelligences.agent_environment.snapshot.building"),
            "building",
        ),
        (
            "snapshot",
            SnapshotStatus("failed", "guildbotics-abc", Path("/snap"), "E: boom"),
            t("intelligences.agent_environment.snapshot.failed", detail="E: boom"),
            "snapshot",
        ),
    ],
)
def test_the_refusal_is_the_first_thing_a_turn_would_stop_on(
    device, part: str, value: object, expected: str, setting: str
) -> None:
    device[part] = value

    status = device_status()

    # The reason and what it is about are one reading, so they never disagree.
    assert (status.refusal, status.setting) == (expected, setting)
    assert not status.ready

    with pytest.raises(AgentRuntimeError) as exc_info:
        _ready("codex")
    assert str(exc_info.value) == status.refusal


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


@pytest.mark.parametrize("error_number", [errno.EPERM, errno.EACCES])
@pytest.mark.parametrize("app", ["Visual Studio Code", ""])
def test_unreadable_exchange_directory_refuses_with_macos_guidance(
    device, monkeypatch, tmp_path, error_number, app
):
    target = tmp_path / "Documents/GuildBotics"
    target.mkdir(parents=True)
    original = module.os.scandir

    def scandir(path):
        if Path(path) == target:
            raise PermissionError(error_number, "denied", str(path))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", scandir)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "launching_app_name", lambda: app)

    status = device_status()
    assert status.setting == "filesystem"
    assert status.refusal == t(
        "intelligences.agent_environment.filesystem.macos_documents",
        app=t("intelligences.agent_environment.filesystem.launching_app", app=app)
        if app
        else "",
    )
    assert not status.ready
    with pytest.raises(AgentRuntimeError) as exc_info:
        _ready("codex")
    assert str(exc_info.value) == status.refusal


def test_grant_preflight_checks_parents_without_creating_directories(
    device, monkeypatch, tmp_path
):
    documents = tmp_path / "Documents"
    documents.mkdir()
    original = module.os.scandir

    def scandir(path):
        if Path(path) == documents:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", scandir)
    monkeypatch.setattr(module.sys, "platform", "linux")
    status = device_status()
    assert status.setting == "filesystem"
    assert status.refusal == t(
        "intelligences.agent_environment.filesystem.permission_denied", path=documents
    )
    assert not (documents / "GuildBotics").exists()


@pytest.mark.parametrize("scope", ["document", "local", "denied"])
def test_preflight_checks_all_open_grants(device, monkeypatch, tmp_path, scope):
    target = tmp_path / "extra"
    target.mkdir()
    monkeypatch.setattr(
        module,
        "load_shared_grants",
        lambda: SharedGrants(
            documents=[DocumentGrant(path="extra", access="read")]
            if scope == "document"
            else []
        ),
    )
    monkeypatch.setattr(
        module,
        "load_local_grants",
        lambda: LocalGrants(
            paths=[LocalPathGrant(path=str(target), access="read")]
            if scope != "document"
            else [],
            deny=[str(target)] if scope == "denied" else [],
        ),
    )
    original = module.os.scandir

    def scandir(path):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", scandir)
    status = device_status()
    assert status.ready == (scope == "denied")
    if scope != "denied":
        assert status.setting == "filesystem"
        assert str(target) in status.refusal


@pytest.mark.parametrize("existing_file", [False, True])
def test_preflight_leaves_missing_local_grants_for_their_row(
    device, monkeypatch, tmp_path, existing_file
):
    target = tmp_path / "local-grant"
    if existing_file:
        target.write_text("not a directory")
    monkeypatch.setattr(
        module,
        "load_local_grants",
        lambda: LocalGrants(paths=[LocalPathGrant(path=str(target), access="read")]),
    )
    status = device_status()
    assert status.ready
    assert not status.access.paths[0].present


def test_preflight_reports_filesystem_changes_during_enumeration(
    device, monkeypatch, tmp_path
):
    target = tmp_path / "Documents/GuildBotics"
    target.mkdir(parents=True)
    error = NotADirectoryError(errno.ENOTDIR, "changed into a file", str(target))

    def scandir(path):
        raise error

    monkeypatch.setattr(module.os, "scandir", scandir)
    status = device_status()
    assert status.setting == "filesystem"
    assert status.refusal == t(
        "intelligences.agent_environment.filesystem.unavailable",
        path=target,
        error=error,
    )


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("language", ["en", "ja"])
def test_login_guidance_quotes_unix_paths_and_uses_windows_path(
    monkeypatch, tmp_path, platform, language
):
    import shlex
    from guildbotics.intelligences.agent_environment.status import ToolStatus
    from guildbotics.utils.i18n_tool import set_language

    home = tmp_path / "A user's home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module.sys, "platform", platform)
    set_language(language)
    command = login_command("codex")
    if platform == "win32":
        assert command == "guildbotics environment login codex"
    else:
        assert shlex.split(command) == [
            str(home / ".guildbotics/bin/guildbotics"),
            "environment",
            "login",
            "codex",
        ]
    for saved in (False, True):
        tool = ToolStatus("codex", "Codex", True, saved, authentication_failed=True)
        assert f"`{command}`" in tool.problem
