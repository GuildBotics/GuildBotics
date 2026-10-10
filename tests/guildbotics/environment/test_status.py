"""One reading of the device answers the CLI, the Desktop, and a refused turn."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.environment import status as module
from guildbotics.environment.command_environment import _ready
from guildbotics.environment.contract import (
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    SharedGrants,
)
from guildbotics.environment.image import (
    ImageStatus,
    image_load_command,
)
from guildbotics.environment.runtime import AgentEnvironmentHealth
from guildbotics.environment.snapshot import SnapshotStatus
from guildbotics.environment.status import (
    device_status,
    login_command,
)
from guildbotics.environment.toolchain import (
    DnsSettings,
    ToolchainDeclaration,
    ToolchainError,
)
from guildbotics.intelligences.agent_runtime.models import AgentRuntimeError
from guildbotics.intelligences.cli_agents import CliAgentInfo
from guildbotics.utils import processes, safe_paths
from guildbotics.utils.i18n_tool import t


@pytest.fixture
def permission_platform(monkeypatch):
    """Simulate message wording without changing native filesystem probes."""
    original = safe_paths.filesystem_permission_problem

    def configure(platform):
        def permission(path):
            with monkeypatch.context() as patch:
                patch.setattr(safe_paths, "sys", SimpleNamespace(platform=platform))
                return original(path)

        monkeypatch.setattr(safe_paths, "filesystem_permission_problem", permission)
        monkeypatch.setattr(module, "filesystem_permission_problem", permission)

    return configure


def test_permission_platform_preserves_native_path_inspection(
    tmp_path, permission_platform
):
    permission_platform("darwin")
    assert safe_paths.sys is sys
    assert safe_paths.inspect_host_path(tmp_path).present
    safe_paths.filesystem_permission_problem(tmp_path)
    assert safe_paths.sys is sys


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, object]:
    """A device whose parts a test can swap one at a time."""
    parts: dict[str, object] = {
        "health": AgentEnvironmentHealth(True, "", "0.6.17"),
        "declaration": ToolchainDeclaration(dns=DnsSettings(nameservers="host")),
        "snapshot": SnapshotStatus("ready", "guildbotics-abc", Path("/snap")),
        "image": ImageStatus(),
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
    monkeypatch.setattr(
        module.snapshot, "snapshot_status", lambda image: parts["snapshot"]
    )
    monkeypatch.setattr(module, "image_status", lambda d, lookup=True: parts["image"])
    monkeypatch.setattr(module, "upstream_nameservers", resolvers)
    monkeypatch.setattr(
        module,
        "credential_state",
        lambda agent: (
            "saved" if agent.name in parts["credentials_saved"] else "missing"
        ),
    )
    return parts


@pytest.mark.parametrize("state", ["locked", "unavailable", "corrupt"])
def test_a_login_that_cannot_be_opened_refuses_the_tool_with_its_reason(
    device, monkeypatch, state
) -> None:
    """A locked keychain or a broken record is not a missing login: the
    status says which, in the words a refused turn gets."""
    monkeypatch.setattr(
        module,
        "credential_state",
        lambda agent: state if agent.name == "claude" else "saved",
    )

    claude = device_status().tool("claude")

    assert not claude.credentials_saved
    assert (
        claude.refusal
        == claude.problem
        == t(
            f"intelligences.agent_environment.tool.credentials_{state}",
            tool="Claude Code",
            command=login_command("claude"),
        )
    )
    assert device_status().tool("codex").refusal == ""


def test_a_ready_device_refuses_nothing_but_a_missing_login(device) -> None:
    status = device_status()

    assert status.ready and (status.refusal, status.setting) == ("", "")
    assert status.warning == ""
    assert status.network == status.declaration.network
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


def test_a_turn_is_refused_by_the_device_first_then_by_its_tool(device) -> None:
    """What verify and the command requirements ask: can this tool start here."""
    status = device_status()

    assert status.turn_refusal("codex") == ""
    assert status.turn_refusal("claude") == status.tool("claude").refusal != ""

    device["health"] = AgentEnvironmentHealth(False, "no hypervisor")
    status = device_status()

    assert status.turn_refusal("codex") == status.turn_refusal("claude")
    assert status.turn_refusal("codex") == "no hypervisor"


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
            "image",
            ImageStatus(
                "local/agent:1", "arm64", "sha256:" + "c" * 64, False, error="locked"
            ),
            "locked",
            "image",
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


def test_an_image_that_differs_from_the_declaration_is_a_warning_not_a_refusal(
    device,
) -> None:
    device["image"] = ImageStatus(
        "local/agent:1", "arm64", "sha256:" + "c" * 64, False, "sha256:" + "d" * 64
    )

    status = device_status()

    assert status.ready and (status.refusal, status.setting) == ("", "")
    assert status.warning == status.image.warning != ""
    _ready("codex")


def test_the_image_is_read_before_the_snapshot_that_would_be_built_from_it(
    device,
) -> None:
    """A missing snapshot is not what to fix while its image is not here."""
    device["snapshot"] = SnapshotStatus("missing", "guildbotics-abc", Path("/snap"))
    device["image"] = ImageStatus("local/agent:1", "arm64", "sha256:" + "c" * 64, False)

    status = device_status()

    assert (status.refusal, status.setting) == (
        t(
            "intelligences.agent_environment.image.missing",
            reference="local/agent:1",
            architecture="arm64",
            command=image_load_command(),
        ),
        "image",
    )


def test_without_a_runtime_the_declared_image_is_shown_but_not_looked_for(
    device, monkeypatch: pytest.MonkeyPatch
) -> None:
    device["health"] = AgentEnvironmentHealth(False, "no hypervisor")
    declared = ImageStatus("local/agent:1", "arm64", "sha256:" + "c" * 64, False)
    lookups: list[bool] = []

    def status_of(declaration, *, lookup: bool = True) -> ImageStatus:
        lookups.append(lookup)
        return declared

    monkeypatch.setattr(module, "image_status", status_of)

    status = device_status()

    assert (status.image, lookups) == (declared, [False])
    assert (status.refusal, status.setting) == ("no hypervisor", "runtime")


def test_an_unreadable_declaration_leaves_no_snapshot_or_dns_to_report(device) -> None:
    device["declaration"] = ToolchainError("agent_environment.yml: bad")

    status = device_status()

    assert status.snapshot is None
    assert status.declaration_problem == "agent_environment.yml: bad"
    assert status.network is None
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
    device, monkeypatch, tmp_path, error_number, app, permission_platform
):
    target = tmp_path / "Documents/GuildBotics"
    target.mkdir(parents=True)
    original = module.os.scandir

    def scandir(path):
        if Path(path) == target:
            raise PermissionError(error_number, "denied", str(path))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", scandir)
    permission_platform("darwin")
    monkeypatch.setattr(processes, "launching_app_name", lambda: app)

    status = device_status()
    assert status.setting == "filesystem"
    assert status.refusal == t(
        "intelligences.agent_environment.filesystem.macos_documents",
        path=target,
        app=t("intelligences.agent_environment.filesystem.launching_app", app=app)
        if app
        else "",
    )
    assert not status.ready
    with pytest.raises(AgentRuntimeError) as exc_info:
        _ready("codex")
    assert str(exc_info.value) == status.refusal


def test_grant_preflight_checks_parents_without_creating_directories(
    device, monkeypatch, tmp_path, permission_platform
):
    documents = tmp_path / "Documents"
    documents.mkdir()
    original = module.os.scandir

    def scandir(path):
        if Path(path) == documents:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", scandir)
    permission_platform("linux")
    status = device_status()
    assert status.setting == "filesystem"
    assert status.refusal == t(
        "intelligences.agent_environment.filesystem.permission_denied", path=documents
    )
    assert not (documents / "GuildBotics").exists()


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory-relative permission failure"
)
def test_nofollow_open_permission_error_keeps_macos_guidance(
    device, monkeypatch, tmp_path, permission_platform
):
    from guildbotics.utils import safe_paths

    target = tmp_path / "Documents"
    target.mkdir()
    original = safe_paths.os.open

    def open_path(path, *args, **kwargs):
        if path == "Documents":
            raise PermissionError(errno.EACCES, "denied", "Documents")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(safe_paths.os, "open", open_path)
    permission_platform("darwin")
    monkeypatch.setattr(processes, "launching_app_name", lambda: "")
    assert device_status().refusal == t(
        "intelligences.agent_environment.filesystem.macos_documents",
        path=target,
        app="",
    )


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
    assert not status.ready
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
    assert status.ready is not existing_file
    if not existing_file:
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
    monkeypatch, tmp_path, platform, language, fake_platform
):
    import shlex

    from guildbotics.environment import provider_state
    from guildbotics.environment.status import ToolStatus
    from guildbotics.utils.i18n_tool import set_language

    home = tmp_path / "A user's home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    fake_platform(provider_state, platform)
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
        tool = ToolStatus(
            "codex",
            "Codex",
            True,
            "saved" if saved else "missing",
            authentication_failed=True,
        )
        assert f"`{command}`" in tool.problem
