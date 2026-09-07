"""The snapshot is named after what it holds, and its state is read off disk."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import runtime, snapshot
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    AgentEnvironmentHealth,
    BuildStep,
)
from guildbotics.intelligences.agent_environment.snapshot import (
    SNAPSHOT_PREFIX,
    SnapshotUpkeep,
    build_snapshot,
    build_steps,
    remove_snapshots,
    snapshot_name,
    snapshot_status,
    snapshots_dir,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    parse_toolchain,
)
from guildbotics.utils.advisory_lock import held_lock


def _declaration(**packages: list[str]) -> ToolchainDeclaration:
    return parse_toolchain(
        {"packages": packages, "dns": {"nameservers": ["10.0.0.53"]}}, where="t"
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    (root / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(root / ".guildbotics" / "config"))
    return root


class _FakeBuild:
    """Stands in for the runtime: records the build and writes the snapshot."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.removed: list[Path] = []
        self.fail = fail
        monkeypatch.setattr(runtime, "build_snapshot", self.build)
        monkeypatch.setattr(runtime, "remove_snapshot", self.remove)

    async def build(self, name: str, **kwargs: Any) -> Path:
        self.calls.append({"name": name, **kwargs})
        kwargs["on_line"]("Get:1 http://deb.debian.org bookworm InRelease")
        if self.fail:
            raise AgentEnvironmentError(self.fail)
        path = kwargs["dest_dir"] / name
        path.mkdir()
        return path

    async def remove(self, path: Path) -> None:
        self.removed.append(path)
        path.rmdir()


# --- naming ----------------------------------------------------------------------


def test_the_name_is_a_digest_of_the_declared_packages_and_the_recipe() -> None:
    plain = snapshot_name(_declaration())

    assert plain.startswith(SNAPSHOT_PREFIX)
    assert snapshot_name(_declaration()) == plain
    assert snapshot_name(_declaration(apt=["ripgrep=14.1.0-1"])) != plain


def test_dns_is_not_part_of_the_name() -> None:
    """The resolvers are a gateway setting of every turn, not snapshot content;
    changing them must not rebuild anything."""
    other = parse_toolchain({"dns": {"nameservers": ["1.1.1.1"]}}, where="t")

    assert snapshot_name(other) == snapshot_name(_declaration())


def test_the_name_changes_with_the_pinned_provider_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot_name(_declaration())
    monkeypatch.setattr(
        snapshot, "provisioned_packages", lambda: {"codex": "@openai/codex@9.9.9"}
    )

    assert snapshot_name(_declaration()) != before


# --- recipe ----------------------------------------------------------------------


def test_the_recipe_installs_the_providers_and_the_declared_packages() -> None:
    steps = build_steps(
        _declaration(
            apt=["ripgrep=14.1.0-1"], npm=["typescript@5.6.3"], uv=["ruff==0.6.9"]
        )
    )

    assert [step.label for step in steps] == ["home", "apt", "uv", "npm", "uv-tools"]
    scripts = {step.label: step.script for step in steps}
    assert 'install -d -m 0700 "$HOME"' in scripts["home"]
    assert (
        "apt-get install -y --no-install-recommends ripgrep=14.1.0-1" in scripts["apt"]
    )
    assert "rm -rf /var/lib/apt/lists/*" in scripts["apt"]
    assert f"astral-sh/uv/releases/download/{snapshot.UV_VERSION}/" in scripts["uv"]
    assert "npm install -g @openai/codex@" in scripts["npm"]
    assert "typescript@5.6.3" in scripts["npm"]
    assert scripts["uv-tools"] == (
        "UV_TOOL_BIN_DIR=/usr/local/bin uv tool install ruff==0.6.9"
    )


def test_an_empty_declaration_still_installs_uv_and_the_providers() -> None:
    assert [s.label for s in build_steps(_declaration())] == ["home", "uv", "npm"]


def test_package_arguments_are_shell_quoted() -> None:
    steps = build_steps(_declaration(npm=["@scope/pkg@1.0.0", "it's"]))

    assert "'it'\"'\"'s'" in {s.label: s.script for s in steps}["npm"]


# --- status ----------------------------------------------------------------------


def test_a_device_without_a_snapshot_is_missing(workspace: Path) -> None:
    status = snapshot_status(_declaration(), workspace)

    assert status.state == "missing"
    assert status.path == snapshots_dir(workspace) / status.name


def test_a_snapshot_of_another_declaration_is_stale(workspace: Path) -> None:
    (snapshots_dir(workspace) / f"{SNAPSHOT_PREFIX}0123456789abcdef").mkdir(
        parents=True
    )

    assert snapshot_status(_declaration(), workspace).state == "stale"


def test_the_named_snapshot_is_ready(workspace: Path) -> None:
    declaration = _declaration()
    (snapshots_dir(workspace) / snapshot_name(declaration)).mkdir(parents=True)

    assert snapshot_status(declaration, workspace).state == "ready"


def test_a_failed_build_is_reported_with_its_reason(workspace: Path) -> None:
    declaration = _declaration()
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)
    (directory / f"{snapshot_name(declaration)}.failed").write_text("npm exploded\n")

    status = snapshot_status(declaration, workspace)

    assert (status.state, status.detail) == ("failed", "npm exploded")


def test_a_held_build_lock_means_building(workspace: Path) -> None:
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)

    with held_lock(directory / "build.lock"):
        assert snapshot_status(_declaration(), workspace).state == "building"
    assert snapshot_status(_declaration(), workspace).state == "missing"


# --- build -----------------------------------------------------------------------


def test_a_build_runs_the_recipe_and_replaces_older_snapshots(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeBuild(monkeypatch)
    directory = snapshots_dir(workspace)
    old = directory / f"{SNAPSHOT_PREFIX}0123456789abcdef"
    old.mkdir(parents=True)
    (directory / f"{SNAPSHOT_PREFIX}0123456789abcdef.failed").write_text("old\n")
    declaration = _declaration(npm=["typescript@5.6.3"])
    lines: list[str] = []
    home = tmp_path / "home"

    status = asyncio.run(
        build_snapshot(
            declaration, on_line=lines.append, workspace_root=workspace, home=home
        )
    )

    call = fake.calls[0]
    assert call["name"] == status.name == snapshot_name(declaration)
    assert call["dest_dir"] == directory
    assert call["image"] == snapshot.IMAGE
    assert call["home"] == home.resolve().as_posix()
    assert call["nameservers"] == ["10.0.0.53"]
    assert [s.label for s in call["steps"]] == ["home", "uv", "npm"]
    assert status.state == "ready"
    assert status.path.is_dir()
    assert fake.removed == [old]
    assert sorted(p.name for p in directory.iterdir()) == ["build.lock", status.name]
    assert lines == ["Get:1 http://deb.debian.org bookworm InRelease"]
    assert snapshot_status(declaration, workspace).state == "ready"


def test_a_failed_build_leaves_its_reason_and_the_older_snapshot(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeBuild(monkeypatch, fail="Build step 'npm' failed with exit code 1.")
    old = snapshots_dir(workspace) / f"{SNAPSHOT_PREFIX}0123456789abcdef"
    old.mkdir(parents=True)
    declaration = _declaration()

    with pytest.raises(AgentEnvironmentError, match="step 'npm'"):
        asyncio.run(
            build_snapshot(
                declaration, on_line=lambda _: None, workspace_root=workspace
            )
        )

    status = snapshot_status(declaration, workspace)
    assert (status.state, status.detail) == (
        "failed",
        "Build step 'npm' failed with exit code 1.",
    )
    assert old.is_dir() and fake.removed == []


def test_a_stalled_build_is_failed_with_the_lock_released(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stall(name: str, **kwargs: Any) -> Path:
        await asyncio.sleep(10)
        return kwargs["dest_dir"] / name

    monkeypatch.setattr(runtime, "build_snapshot", stall)
    monkeypatch.setattr(snapshot, "BUILD_TIMEOUT_SECONDS", 0.05)
    declaration = _declaration()

    with pytest.raises(AgentEnvironmentError, match="did not finish within 0 minutes"):
        asyncio.run(
            build_snapshot(
                declaration, on_line=lambda _: None, workspace_root=workspace
            )
        )

    status = snapshot_status(declaration, workspace)
    assert status.state == "failed"
    assert "did not finish" in status.detail


def test_a_second_build_is_refused_while_one_runs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeBuild(monkeypatch)
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)

    with held_lock(directory / "build.lock"):
        with pytest.raises(AgentEnvironmentError, match="already running"):
            asyncio.run(
                build_snapshot(
                    _declaration(), on_line=lambda _: None, workspace_root=workspace
                )
            )


def test_removing_drops_every_snapshot_and_marker(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeBuild(monkeypatch)
    directory = snapshots_dir(workspace)
    for name in ("a", "b"):
        (directory / f"{SNAPSHOT_PREFIX}{name}").mkdir(parents=True)
    (directory / f"{SNAPSHOT_PREFIX}a.failed").write_text("x\n")

    removed = asyncio.run(remove_snapshots(workspace))

    assert removed == [f"{SNAPSHOT_PREFIX}a", f"{SNAPSHOT_PREFIX}b"]
    assert len(fake.removed) == 2
    assert list(directory.iterdir()) == []
    assert asyncio.run(remove_snapshots(workspace)) == []


# --- upkeep ----------------------------------------------------------------------


def _upkeep(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> SnapshotUpkeep:
    caplog.set_level(logging.INFO)
    return SnapshotUpkeep(threading.Event(), logging.getLogger("test-upkeep"))


def test_the_service_builds_a_missing_snapshot_by_itself(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeBuild(monkeypatch)
    monkeypatch.setattr(runtime, "doctor", lambda: AgentEnvironmentHealth(True))
    upkeep = _upkeep(monkeypatch, caplog)

    upkeep.once()
    upkeep.once()

    assert len(fake.calls) == 1
    assert "is ready" in caplog.text


def test_the_service_leaves_a_failed_build_alone(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeBuild(monkeypatch, fail="no network")
    monkeypatch.setattr(runtime, "doctor", lambda: AgentEnvironmentHealth(True))
    upkeep = _upkeep(monkeypatch, caplog)

    upkeep.once()
    upkeep.once()

    assert len(fake.calls) == 1
    assert "build failed: no network" in caplog.text


def test_the_service_says_once_why_it_cannot_build(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeBuild(monkeypatch)
    monkeypatch.setattr(
        runtime, "doctor", lambda: AgentEnvironmentHealth(False, "no hypervisor")
    )
    upkeep = _upkeep(monkeypatch, caplog)

    upkeep.once()
    upkeep.once()

    assert fake.calls == []
    assert caplog.text.count("no hypervisor") == 1


def test_a_broken_declaration_stops_the_service_from_building(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeBuild(monkeypatch)
    monkeypatch.setattr(runtime, "doctor", lambda: AgentEnvironmentHealth(True))
    target = workspace / ".guildbotics" / "config" / "intelligences"
    target.mkdir(parents=True)
    (target / "agent_environment.yml").write_text("dns:\n  nameservers: [nope]\n")
    upkeep = _upkeep(monkeypatch, caplog)

    upkeep.once()

    assert fake.calls == []
    assert "cannot be read" in caplog.text


def test_the_build_step_type_is_what_the_recipe_hands_the_runtime() -> None:
    assert isinstance(build_steps(_declaration())[0], BuildStep)
