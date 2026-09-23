"""The ``guildbotics environment`` group drives the snapshot and login modules."""

from __future__ import annotations

import base64
import importlib
import json

import yaml
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

environment_cli = importlib.import_module("guildbotics.cli.environment")
from guildbotics.intelligences.agent_environment.status import login_command
from guildbotics.cli.environment import environment as environment_group
from guildbotics.cli import main
from guildbotics.intelligences.agent_environment import (
    provider_state,
    runtime,
    snapshot,
)
from guildbotics.intelligences.agent_environment import image as image_module
from guildbotics.intelligences.agent_environment.image import (
    ImageStatus,
    image_load_command,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    AgentEnvironmentHealth,
    ImageInfo,
)
from guildbotics.intelligences.agent_environment.snapshot import (
    SnapshotStatus,
    snapshot_name,
)
from guildbotics.intelligences.cli_agents import cli_agent_info
from guildbotics.utils.i18n_tool import t


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    root = tmp_path / "workspace"
    (root / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.setattr(
        provider_state,
        "get_machine_state_path",
        lambda *parts: tmp_path.joinpath("data", *parts),
    )
    monkeypatch.setattr(
        runtime, "doctor", lambda: AgentEnvironmentHealth(True, "", "0.6.17")
    )
    monkeypatch.setattr(image_module.platform, "machine", lambda: "arm64")
    return root


def _save_codex_login() -> None:
    """Seal a synthetic Codex login, as `environment login codex` would."""
    tool = cli_agent_info("codex")
    claims = base64.urlsafe_b64encode(b'{"exp": 4102444800}').rstrip(b"=").decode()
    login = {"tokens": {"access_token": f"e30.{claims}.c2ln", "refresh_token": "r"}}
    provider_state._seal_login(tool, {tool.provision.auth: json.dumps(login).encode()})


def _invoke(workspace: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        main, ["environment", "--workspace", str(workspace), *arguments]
    )


def test_status_reports_runtime_snapshot_and_logins(workspace: Path) -> None:
    result = _invoke(workspace, "status")

    assert result.exit_code == 0, result.output
    assert "runtime: available 0.6.17" in result.output
    assert "resources: 4096 MiB, 2 vCPU" in result.output
    assert f"image: {image_module.IMAGE} (GuildBotics default)" in result.output
    assert (
        f"snapshot: missing {snapshot_name(ImageStatus())} "
        "(run `guildbotics environment build`)"
    ) in result.output
    assert "dns: 1.1.1.1, 8.8.8.8 -> 1.1.1.1, 8.8.8.8" in result.output
    assert "network: allowlist (pypi.org" in result.output
    for name, label in (("codex", "Codex"), ("grok", "Grok Build")):
        assert (
            t(
                "intelligences.agent_environment.tool.credentials_missing",
                tool=label,
                command=login_command(name),
            )
            in result.output
        )


def test_status_json_has_the_same_facts(workspace: Path) -> None:
    result = _invoke(workspace, "status", "--format", "json")

    payload = json.loads(result.output)
    assert payload["runtime"] == {
        "available": True,
        "reason": "",
        "version": "0.6.17",
        "home": "",
    }
    assert payload["resources"] == {"memory_mib": 4096, "cpus": 2}
    assert payload["snapshot"]["state"] == "missing"
    assert payload["warning"] == ""
    assert payload["image"] == {
        "reference": "",
        "architecture": "arm64",
        "digest": "",
        "digests": {},
        "present": True,
        "held": "",
        "problem": "",
        "warning": "",
    }
    assert payload["dns"] == {
        "declared": "1.1.1.1, 8.8.8.8",
        "nameservers": ["1.1.1.1", "8.8.8.8"],
        "problem": "",
    }
    assert payload["network"]["mode"] == "allowlist"
    assert "api.github.com" in payload["network"]["allowed_domains"]
    tools = {tool["name"]: tool for tool in payload["tools"]}
    assert tools["codex"] == {
        "name": "codex",
        "label": "Codex",
        "provisioned": True,
        "credentials_saved": False,
        "authentication_failed": False,
        "problem": t(
            "intelligences.agent_environment.tool.credentials_missing",
            tool="Codex",
            command=login_command("codex"),
        ),
    }
    assert tools["antigravity"]["provisioned"] is True


def test_an_unreadable_declaration_does_not_look_like_deny(workspace: Path) -> None:
    declaration = workspace / ".guildbotics/config/intelligences/agent_environment.yml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text("network: []\ndns:\n  nameservers: [1.1.1.1]\n")

    text = _invoke(workspace, "status")
    payload = _invoke(workspace, "status", "--format", "json")

    assert text.exit_code == payload.exit_code == 0
    assert "network: unavailable" in text.output
    assert "resources: unavailable" in text.output
    assert json.loads(payload.output)["network"] is None
    assert json.loads(payload.output)["resources"] is None


def test_status_displays_the_device_filesystem_refusal(workspace, monkeypatch):
    _invoke(workspace, "status")
    reason = t("intelligences.agent_environment.filesystem.macos_documents", app="")
    status = replace(
        environment_cli.device_status(),
        snapshot=SnapshotStatus("ready", "test", Path("/snap")),
        filesystem_problem=reason,
    )
    monkeypatch.setattr(environment_cli, "device_status", lambda: status)
    assert reason in _invoke(workspace, "status").output
    payload = json.loads(_invoke(workspace, "status", "--format", "json").output)
    assert (payload["refusal"], payload["setting"]) == (reason, "filesystem")


def test_commands_refuse_without_a_runtime(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime, "doctor", lambda: AgentEnvironmentHealth(False, "no hypervisor")
    )

    for command in ("build", "remove"):
        result = _invoke(workspace, command)
        assert result.exit_code != 0
        assert "no hypervisor" in result.output
    assert "runtime: unavailable: no hypervisor" in _invoke(workspace, "status").output


def test_build_runs_the_recipe_and_prints_its_lines(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(declaration: Any, *, on_line: Any, **_: Any) -> SnapshotStatus:
        on_line("[npm]")
        return SnapshotStatus("ready", "guildbotics-abc", Path("/snap"))

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    result = _invoke(workspace, "build")

    assert result.exit_code == 0, result.output
    assert "[npm]" in result.output
    assert "guildbotics-abc is ready" in result.output


def test_build_does_nothing_when_up_to_date_unless_forced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = snapshot_name(ImageStatus())
    (snapshot.snapshots_dir(workspace) / name).mkdir(parents=True)
    built: list[str] = []

    async def fake_build(declaration: Any, *, on_line: Any, **_: Any) -> SnapshotStatus:
        built.append(name)
        return SnapshotStatus("ready", name, Path("/snap"))

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    assert "already up to date" in _invoke(workspace, "build").output
    assert built == []
    assert _invoke(workspace, "build", "--force").exit_code == 0
    assert built == [name]


def test_build_failure_is_an_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(*_: Any, **__: Any) -> SnapshotStatus:
        raise AgentEnvironmentError("Build step 'apt' failed with exit code 100.")

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    result = _invoke(workspace, "build")

    assert result.exit_code != 0
    assert "step 'apt' failed" in result.output


def test_login_needs_a_ready_snapshot(workspace: Path) -> None:
    result = _invoke(workspace, "login", "codex")

    assert result.exit_code != 0
    assert "The environment is missing; build it first" in result.output


def test_login_accepts_only_provisioned_tools(workspace: Path) -> None:
    result = _invoke(workspace, "login", "ghost")

    assert result.exit_code != 0
    assert (
        "Invalid value for '{codex|claude|grok|copilot|antigravity}'" in result.output
    )


def test_login_runs_inside_the_ready_snapshot_and_confirms_the_store(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = snapshot_name(ImageStatus())
    path = snapshot.snapshots_dir(workspace) / name
    path.mkdir(parents=True)
    calls: list[dict[str, Any]] = []

    async def fake_login(tool: Any, declaration: Any, **kwargs: Any) -> int:
        calls.append({"tool": tool.name, **kwargs})
        kwargs["write"]("Logged in\n")
        _save_codex_login()
        return 0

    monkeypatch.setattr(provider_state, "login", fake_login)

    result = _invoke(workspace, "login", "codex")

    assert result.exit_code == 0, result.output
    assert calls[0]["tool"] == "codex" and calls[0]["snapshot"] == path
    assert "Logged in" in result.output
    saved = t("intelligences.agent_environment.tool.credentials_saved", tool="Codex")
    assert saved in result.output
    assert saved in _invoke(workspace, "status").output


def test_login_that_stores_nothing_is_an_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = snapshot.snapshots_dir(workspace) / snapshot_name(ImageStatus())
    path.mkdir(parents=True)

    async def fake_login(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(provider_state, "login", fake_login)

    result = _invoke(workspace, "login", "claude")

    assert result.exit_code != 0
    assert "stored no credentials" in result.output


def test_remove_lists_what_it_dropped(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_remove(*_: Any, **__: Any) -> list[str]:
        return ["guildbotics-old"]

    monkeypatch.setattr(snapshot, "remove_snapshots", fake_remove)

    assert "removed guildbotics-old" in _invoke(workspace, "remove").output


def test_environment_group_lists_its_commands() -> None:
    result = CliRunner().invoke(environment_group, ["--help"])

    assert result.exit_code == 0
    for command in ("build", "image", "login", "remove", "status"):
        assert f"\n  {command}" in result.output


_DIGEST = "sha256:" + "c" * 64


def _declare_image(workspace: Path, digest: str = _DIGEST) -> None:
    target = workspace / ".guildbotics" / "config" / "intelligences"
    target.mkdir(parents=True, exist_ok=True)
    (target / "agent_environment.yml").write_text(
        f"image:\n  reference: local/agent:1\n  digests:\n    arm64: {digest}\n"
        "dns:\n  nameservers: [1.1.1.1]\n"
    )


def test_status_reports_the_declared_image_as_this_device_holds_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_image(workspace)
    monkeypatch.setattr(runtime, "list_images", lambda: ())

    missing = _invoke(workspace, "status")
    problem = t(
        "intelligences.agent_environment.image.missing",
        reference="local/agent:1",
        architecture="arm64",
        command=image_load_command(),
    )
    assert f"image: local/agent:1 [arm64={_DIGEST[:19]}] arm64: not loaded" in (
        missing.output
    )
    assert f"image: {problem}" in missing.output

    monkeypatch.setattr(
        runtime,
        "list_images",
        lambda: (ImageInfo("local/agent:1", _DIGEST, "arm64"),),
    )
    loaded = _invoke(workspace, "status", "--format", "json")
    payload = json.loads(loaded.output)
    assert payload["image"] == {
        "reference": "local/agent:1",
        "architecture": "arm64",
        "digest": _DIGEST,
        "digests": {"arm64": _DIGEST},
        "present": True,
        "held": _DIGEST,
        "problem": "",
        "warning": "",
    }
    # The snapshot is still to be built; the image itself refuses nothing.
    assert payload["refusal"] == t("intelligences.agent_environment.snapshot.missing")
    assert payload["warning"] == ""

    other = "sha256:" + "d" * 64
    monkeypatch.setattr(
        runtime, "list_images", lambda: (ImageInfo("local/agent:1", other, "arm64"),)
    )
    differs = _invoke(workspace, "status")
    warning = t(
        "intelligences.agent_environment.image.mismatch",
        reference="local/agent:1",
        architecture="arm64",
        held=other[:19],
        digest=_DIGEST[:19],
        command=image_load_command(),
    )
    assert f"warning: {warning}" in differs.output
    assert (
        f"image: local/agent:1 [arm64={_DIGEST[:19]}] arm64: loaded at {other[:19]}, "
        "not the declared one"
    ) in differs.output
    assert "image: The base image" not in differs.output

    monkeypatch.setattr(image_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        runtime, "list_images", lambda: (ImageInfo("local/agent:1", other, "amd64"),)
    )
    undeclared = _invoke(workspace, "status")
    assert (
        f"image: local/agent:1 [arm64={_DIGEST[:19]}] amd64: loaded at {other[:19]}, "
        "not declared for amd64"
    ) in undeclared.output


def test_build_refuses_while_the_declared_image_is_not_here(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_image(workspace)
    monkeypatch.setattr(runtime, "list_images", lambda: ())
    calls: list[str] = []
    monkeypatch.setattr(
        runtime, "build_snapshot", lambda name, **kw: calls.append(name)
    )

    result = _invoke(workspace, "build")

    assert result.exit_code == 1
    assert "local/agent:1" in result.output and "image load" in result.output
    assert calls == []


def test_image_list_prints_what_the_declaration_may_name(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipe's own image, the pull's digest alias, and another
    architecture's image are in the store but not on the list; the row the
    declaration names is marked."""
    _declare_image(workspace)
    other = "sha256:" + "d" * 64
    monkeypatch.setattr(
        runtime,
        "list_images",
        lambda: (
            ImageInfo("node:22.23.2-bookworm", "sha256:" + "a" * 64, "arm64", 381),
            ImageInfo(
                "docker.io/library/node@sha256:" + "a" * 64,
                "sha256:" + "a" * 64,
                "arm64",
            ),
            ImageInfo("local/agent:1", _DIGEST, "arm64", 900),
            ImageInfo("local/agent:1", other, "amd64", 900),
            ImageInfo("local/other:2", other, "arm64", 1),
        ),
    )

    text = _invoke(workspace, "image", "list")
    assert text.output.splitlines() == [
        "architecture: arm64",
        f"local/agent:1 {_DIGEST} declared",
        f"local/other:2 {other}",
    ]

    as_json = _invoke(workspace, "image", "list", "--format", "json")
    assert json.loads(as_json.output) == {
        "architecture": "arm64",
        "images": [
            {
                "reference": "local/agent:1",
                "digest": _DIGEST,
                "size_bytes": 900,
                "declared": True,
            },
            {
                "reference": "local/other:2",
                "digest": other,
                "size_bytes": 1,
                "declared": False,
            },
        ],
    }

    monkeypatch.setattr(
        runtime,
        "list_images",
        lambda: (ImageInfo("local/agent:1", other, "arm64", 900),),
    )
    assert (
        f"local/agent:1 {other} declared at {_DIGEST[:19]}"
        in _invoke(workspace, "image", "list").output
    )
    monkeypatch.setattr(runtime, "list_images", lambda: ())
    (
        workspace
        / ".guildbotics"
        / "config"
        / "intelligences"
        / "agent_environment.yml"
    ).unlink()
    empty = _invoke(workspace, "image", "list")
    assert empty.output.splitlines() == [
        "architecture: arm64",
        "This device holds no image.",
    ]


def _declared_image(workspace: Path) -> dict[str, Any]:
    text = (
        workspace
        / ".guildbotics"
        / "config"
        / "intelligences"
        / "agent_environment.yml"
    ).read_text()
    return yaml.safe_load(text)


def test_image_declare_names_the_loaded_image_for_this_architecture(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime, "list_images", lambda: (ImageInfo("local/agent:1", _DIGEST, "arm64"),)
    )

    result = _invoke(workspace, "image", "declare", "local/agent:1")

    assert result.exit_code == 0, result.output
    assert result.output == f"local/agent:1 arm64 {_DIGEST}\n"
    declared = _declared_image(workspace)
    assert declared["image"] == {
        "reference": "local/agent:1",
        "digests": {"arm64": _DIGEST},
    }
    assert declared["dns"] == {"nameservers": ["1.1.1.1", "8.8.8.8"]}
    assert declared["network"]["mode"] == "allowlist"
    assert "packages" not in declared

    absent = _invoke(workspace, "image", "declare", "local/agent:2")
    assert absent.exit_code == 1
    assert "local/agent:2" in absent.output and "image load" in absent.output


def test_image_declare_merges_digests_of_the_same_reference_and_replaces_another(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit digests need no runtime: a build machine declares every
    architecture it built for. Another reference starts over."""
    monkeypatch.setattr(
        runtime, "doctor", lambda: pytest.fail("no runtime needed for explicit digests")
    )
    _declare_image(workspace)
    other = "sha256:" + "d" * 64

    merged = _invoke(
        workspace, "image", "declare", "local/agent:1", "--digest", f"amd64={other}"
    )
    assert merged.exit_code == 0, merged.output
    assert merged.output.splitlines() == [
        f"local/agent:1 amd64 {other}",
        f"local/agent:1 arm64 {_DIGEST}",
    ]
    assert _declared_image(workspace)["image"]["digests"] == {
        "arm64": _DIGEST,
        "amd64": other,
    }

    replaced = _invoke(
        workspace, "image", "declare", "local/agent:2", "--digest", f"amd64={other}"
    )
    assert replaced.exit_code == 0, replaced.output
    assert _declared_image(workspace)["image"] == {
        "reference": "local/agent:2",
        "digests": {"amd64": other},
    }

    bad = _invoke(workspace, "image", "declare", "local/agent:2", "--digest", "amd64")
    assert bad.exit_code == 2 and "ARCH=DIGEST" in bad.output
    invalid = _invoke(
        workspace, "image", "declare", "local/agent:2", "--digest", "amd64=sha256:x"
    )
    assert invalid.exit_code == 1 and "sha256:x" in invalid.output

    default = _invoke(workspace, "image", "declare", "--default")
    assert default.exit_code == 0, default.output
    assert "image" not in _declared_image(workspace)
    assert _invoke(workspace, "image", "declare").exit_code == 2
    assert _invoke(workspace, "image", "declare", "x:1", "--default").exit_code == 2


def test_image_load_reads_the_archive_and_prints_what_it_added(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "agent.tar"
    archive.write_bytes(b"tar")
    loads: list[dict[str, Any]] = []

    async def load(path: Path, *, tag: str | None = None) -> tuple[ImageInfo, ...]:
        loads.append({"path": path, "tag": tag})
        return (ImageInfo("local/agent:1", _DIGEST, "arm64", 900),)

    monkeypatch.setattr(runtime, "load_image", load)
    monkeypatch.setattr(runtime, "archive_architecture", lambda path: "arm64")

    result = _invoke(workspace, "image", "load", str(archive), "--tag", "local/agent:1")

    assert result.exit_code == 0, result.output
    assert loads == [{"path": archive, "tag": "local/agent:1"}]
    assert result.output == f"loaded local/agent:1 {_DIGEST}\n"

    monkeypatch.setattr(runtime, "archive_architecture", lambda path: "amd64")
    foreign = _invoke(workspace, "image", "load", str(archive))
    assert foreign.exit_code == 1 and "amd64" in foreign.output

    absent = _invoke(workspace, "image", "load", str(tmp_path / "none.tar"))
    assert absent.exit_code == 2


def test_image_commands_report_a_runtime_refusal(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "agent.tar"
    archive.write_bytes(b"tar")

    async def load(path: Path, *, tag: str | None = None) -> tuple[ImageInfo, ...]:
        raise AgentEnvironmentError("not a tar")

    monkeypatch.setattr(runtime, "load_image", load)
    monkeypatch.setattr(runtime, "archive_architecture", lambda path: "")

    def fail() -> tuple[ImageInfo, ...]:
        raise AgentEnvironmentError("store locked")

    monkeypatch.setattr(runtime, "list_images", fail)

    assert "not a tar" in _invoke(workspace, "image", "load", str(archive)).output
    assert "store locked" in _invoke(workspace, "image", "list").output


@pytest.mark.parametrize("language", ["en", "ja"])
def test_status_reports_failed_authentication_without_blocking_retries(
    workspace, language
):
    from guildbotics.utils.i18n_tool import set_language

    set_language(language)
    tool = cli_agent_info("codex")
    _save_codex_login()
    provider_state.record_authentication_outcome(tool, failed=True)
    reason = t(
        "intelligences.agent_environment.tool.authentication_failed",
        tool=tool.label,
        command=login_command(tool.name),
    )
    assert reason in _invoke(workspace, "status").output
    payload = json.loads(_invoke(workspace, "status", "--format", "json").output)
    codex = next(item for item in payload["tools"] if item["name"] == "codex")
    assert codex["credentials_saved"] and codex["authentication_failed"]
    assert codex["problem"] == reason
    assert environment_cli.device_status().tool("codex").refusal == ""
