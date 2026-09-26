"""The snapshot is named after what it holds, and its state is read off disk."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import image as image_module
from guildbotics.intelligences.agent_environment import runtime, snapshot
from guildbotics.intelligences.agent_environment.image import (
    ImageStatus,
    image_load_command,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    AgentEnvironmentHealth,
    BuildStep,
    ImageInfo,
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
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.i18n_tool import t


def _declaration() -> ToolchainDeclaration:
    return parse_toolchain({"dns": {"nameservers": ["10.0.0.53"]}}, where="t")


_DIGEST = "sha256:" + "c" * 64


def _labels() -> list[str]:
    return ["home", "uv", "pdf", "python", "npm", *snapshot.provisioned_installs()]


def _with_image(reference: str = "local/agent:1", digest: str = _DIGEST):
    return parse_toolchain(
        {
            "image": {"reference": reference, "digests": {"arm64": digest}},
            "dns": {"nameservers": ["10.0.0.53"]},
        },
        where="t",
    )


@pytest.fixture(autouse=True)
def arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_module.platform, "machine", lambda: "arm64")


def _device_holds(
    monkeypatch: pytest.MonkeyPatch, images: dict[str, str] | None = None
) -> None:
    """What the runtime's store answers on this device: reference -> digest."""
    held = {"local/agent:1": _DIGEST} if images is None else images
    monkeypatch.setattr(
        runtime,
        "list_images",
        lambda: tuple(
            ImageInfo(reference, digest, "arm64") for reference, digest in held.items()
        ),
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    (root / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(root / ".guildbotics" / "config"))
    return root


class _FakeBuild:
    """Stands in for the runtime: records the build and writes the snapshot.

    ``built_from`` is the config digest the runtime reports having created
    the sandbox from; "" stands for the recipe's own image.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, fail: str = "", built_from: str = ""
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.removed: list[Path] = []
        self.fail = fail
        self.built_from = built_from
        monkeypatch.setattr(runtime, "build_snapshot", self.build)
        monkeypatch.setattr(runtime, "remove_snapshot", self.remove)

    async def build(self, name: Any, **kwargs: Any) -> Path:
        snapshot_name = name(self.built_from)
        self.calls.append({"name": snapshot_name, **kwargs})
        kwargs["on_line"]("Get:1 http://deb.debian.org bookworm InRelease")
        if self.fail:
            raise AgentEnvironmentError(self.fail)
        path = kwargs["dest_dir"] / snapshot_name
        path.mkdir()
        return path

    async def remove(self, path: Path) -> None:
        self.removed.append(path)
        path.rmdir()


# --- naming ----------------------------------------------------------------------


def test_the_name_is_a_digest_of_the_image_and_provider_recipe() -> None:
    plain = snapshot_name(ImageStatus())

    assert plain.startswith(SNAPSHOT_PREFIX)
    assert snapshot_name(ImageStatus()) == plain


def test_a_declared_image_names_the_snapshot_by_the_digest_this_device_holds() -> None:
    """A reference is re-tagged when the image is rebuilt; the digest is
    what the device actually holds, so it is what the name follows."""
    plain = snapshot_name(ImageStatus())
    held = ImageStatus("local/agent:1", "arm64", _DIGEST, True, _DIGEST)
    declared = snapshot_name(held)

    assert declared != plain
    # The snapshot is built from what the device holds, so that names it:
    # an image loaded under the reference at another digest is another
    # snapshot, whatever the declaration says.
    other = ImageStatus("local/agent:1", "arm64", _DIGEST, False, "sha256:" + "d" * 64)
    assert snapshot_name(other) != declared


def test_the_name_changes_with_the_pinned_provider_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot_name(ImageStatus())
    monkeypatch.setattr(
        snapshot, "provisioned_packages", lambda: {"codex": "@openai/codex@9.9.9"}
    )
    changed = snapshot_name(ImageStatus())
    assert changed != before

    # A script-installed tool is pinned by its script, so that counts too.
    monkeypatch.setattr(snapshot, "provisioned_installs", lambda: {"grok": "pin 9.9.9"})
    assert snapshot_name(ImageStatus()) != changed


def test_the_name_changes_with_the_pinned_python_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dependency bumped in ``uv.lock`` reaches the microVM only by a
    rebuild, so the exported list is what the name follows."""
    before = snapshot_name(ImageStatus())
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        snapshot.REQUIREMENTS.read_text(encoding="utf-8").replace(
            "weasyprint==", "weasyprint==0", 1
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot, "REQUIREMENTS", requirements)

    assert snapshot_name(ImageStatus()) != before


def test_the_name_changes_with_the_python_and_its_native_libraries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot_name(ImageStatus())
    monkeypatch.setattr(snapshot, "PYTHON_VERSION", "3.13")
    changed = snapshot_name(ImageStatus())
    assert changed != before

    monkeypatch.setattr(snapshot, "PDF_PACKAGES", ("libpango-1.0-0",))
    assert snapshot_name(ImageStatus()) != changed


# --- recipe ----------------------------------------------------------------------


def test_the_recipe_installs_only_guildbotics_and_the_providers() -> None:
    steps = build_steps()

    installs = list(snapshot.provisioned_installs())
    assert [step.label for step in steps] == _labels()
    scripts = {step.label: step.script for step in steps}
    # A tool that is not on npm is put in by its own pinned script.
    for name in installs:
        assert scripts[name] == snapshot.provisioned_installs()[name]
    assert 'install -d -m 0700 "$HOME"' in scripts["home"]
    assert f"astral-sh/uv/releases/download/{snapshot.UV_VERSION}/" in scripts["uv"]
    assert "npm install -g @openai/codex@" in scripts["npm"]
    for package in snapshot.PDF_PACKAGES:
        assert package in scripts["pdf"]
    # GuildBotics' own Python: the pinned list verbatim, installed into its
    # own environment, and WeasyPrint proven loadable before the snapshot is.
    assert f"uv venv --no-cache --python 3.12 {snapshot.VENV}" in scripts["python"]
    assert snapshot.REQUIREMENTS.read_text(encoding="utf-8") in scripts["python"]
    assert f"-r {snapshot.VENV}/requirements.txt" in scripts["python"]
    assert scripts["python"].endswith("-c 'import weasyprint'")


def test_an_empty_declaration_still_installs_uv_and_the_providers() -> None:
    assert [s.label for s in build_steps()] == _labels()


# --- status ----------------------------------------------------------------------


def test_a_device_without_a_snapshot_is_missing(workspace: Path) -> None:
    status = snapshot_status(ImageStatus(), workspace)

    assert status.state == "missing"
    assert status.path == snapshots_dir(workspace) / status.name


def test_a_snapshot_of_another_declaration_is_stale(workspace: Path) -> None:
    (snapshots_dir(workspace) / f"{SNAPSHOT_PREFIX}0123456789abcdef").mkdir(
        parents=True
    )

    assert snapshot_status(ImageStatus(), workspace).state == "stale"


def test_the_named_snapshot_is_ready(workspace: Path) -> None:
    (snapshots_dir(workspace) / snapshot_name(ImageStatus())).mkdir(parents=True)

    assert snapshot_status(ImageStatus(), workspace).state == "ready"


def test_a_failed_build_is_reported_with_its_reason(workspace: Path) -> None:
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)
    (directory / f"{snapshot_name(ImageStatus())}.failed").write_text("npm exploded\n")

    status = snapshot_status(ImageStatus(), workspace)

    assert (status.state, status.detail) == ("failed", "npm exploded")


def test_a_held_build_lock_means_building(workspace: Path) -> None:
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)

    with held_lock(directory / "build.lock"):
        assert snapshot_status(ImageStatus(), workspace).state == "building"
    assert snapshot_status(ImageStatus(), workspace).state == "missing"


# --- build -----------------------------------------------------------------------


def test_a_build_runs_the_recipe_and_replaces_older_snapshots(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeBuild(monkeypatch)
    directory = snapshots_dir(workspace)
    old = directory / f"{SNAPSHOT_PREFIX}0123456789abcdef"
    old.mkdir(parents=True)
    (directory / f"{SNAPSHOT_PREFIX}0123456789abcdef.failed").write_text("old\n")
    declaration = _declaration()
    lines: list[str] = []
    home = tmp_path / "home"

    status = asyncio.run(
        build_snapshot(
            declaration, on_line=lines.append, workspace_root=workspace, home=home
        )
    )

    call = fake.calls[0]
    assert call["name"] == status.name == snapshot_name(ImageStatus())
    assert call["dest_dir"] == directory
    assert (call["image"], call["pull"]) == (image_module.IMAGE, True)
    assert call["home"] == guest_path(home.resolve())
    assert call["nameservers"] == ("10.0.0.53",)
    assert (call["memory_mib"], call["cpus"]) == (4096, 2)
    assert [s.label for s in call["steps"]] == _labels()
    assert status.state == "ready"
    assert status.path.is_dir()
    assert fake.removed == [old]
    assert sorted(p.name for p in directory.iterdir()) == ["build.lock", status.name]
    assert lines == ["Get:1 http://deb.debian.org bookworm InRelease"]
    assert snapshot_status(ImageStatus(), workspace).state == "ready"


def test_a_build_starts_from_the_declared_image_this_device_holds(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeBuild(monkeypatch, built_from=_DIGEST)
    _device_holds(monkeypatch)

    status = asyncio.run(
        build_snapshot(_with_image(), on_line=lambda _: None, workspace_root=workspace)
    )

    assert (fake.calls[0]["image"], fake.calls[0]["pull"]) == ("local/agent:1", False)
    held = ImageStatus("local/agent:1", "arm64", _DIGEST, True, _DIGEST)
    assert status.name == snapshot_name(held)


def test_a_snapshot_is_named_by_the_image_the_runtime_built_from(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent ``image load`` re-tagged the reference between the
    status read and the build: the content is the newer image's, so the
    name is too, and the declared digest's snapshot is still missing."""
    newer = "sha256:" + "e" * 64
    _FakeBuild(monkeypatch, built_from=newer)
    _device_holds(monkeypatch)

    status = asyncio.run(
        build_snapshot(_with_image(), on_line=lambda _: None, workspace_root=workspace)
    )

    as_read = ImageStatus("local/agent:1", "arm64", _DIGEST, True, _DIGEST)
    as_built = ImageStatus("local/agent:1", "arm64", _DIGEST, False, newer)
    assert status.name == snapshot_name(as_built)
    assert status.name != snapshot_name(as_read)
    assert status.path.is_dir()
    assert snapshot_status(as_read, workspace).state == "stale"


def test_a_build_refuses_a_declared_image_this_device_lacks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is built and nothing is remembered as failed: the device is
    told to load the image, and the next build goes ahead once it has."""
    fake = _FakeBuild(monkeypatch)
    _device_holds(monkeypatch, {})

    with pytest.raises(AgentEnvironmentError) as exc_info:
        asyncio.run(
            build_snapshot(
                _with_image(), on_line=lambda _: None, workspace_root=workspace
            )
        )

    assert str(exc_info.value) == t(
        "intelligences.agent_environment.image.missing",
        reference="local/agent:1",
        architecture="arm64",
        command=image_load_command(),
    )
    assert fake.calls == []
    assert list(snapshots_dir(workspace).glob("*.failed")) == []


def test_a_build_from_an_image_that_differs_from_the_declaration_says_so(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "sha256:" + "d" * 64
    fake = _FakeBuild(monkeypatch, built_from=other)
    _device_holds(monkeypatch, {"local/agent:1": other})
    lines: list[str] = []

    status = asyncio.run(
        build_snapshot(_with_image(), on_line=lines.append, workspace_root=workspace)
    )

    assert (fake.calls[0]["image"], fake.calls[0]["pull"]) == ("local/agent:1", False)
    held = ImageStatus("local/agent:1", "arm64", _DIGEST, False, other)
    assert status.name == snapshot_name(held)
    assert lines[0] == "[image] " + t(
        "intelligences.agent_environment.image.mismatch",
        reference="local/agent:1",
        architecture="arm64",
        held=other[:19],
        digest=_DIGEST[:19],
        command=image_load_command(),
    )


def test_a_build_reads_the_devices_resolvers_when_the_declaration_says_host(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeBuild(monkeypatch)
    monkeypatch.setattr(snapshot, "upstream_nameservers", lambda dns: ("192.168.3.1",))
    declaration = parse_toolchain({"dns": {"nameservers": "host"}}, where="t")

    asyncio.run(
        build_snapshot(declaration, on_line=lambda _: None, workspace_root=workspace)
    )

    assert fake.calls[0]["nameservers"] == ("192.168.3.1",)


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

    status = snapshot_status(ImageStatus(), workspace)
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

    timed_out = t("intelligences.agent_environment.snapshot.build_timeout", minutes=0)
    with pytest.raises(AgentEnvironmentError, match=re.escape(timed_out)):
        asyncio.run(
            build_snapshot(
                declaration, on_line=lambda _: None, workspace_root=workspace
            )
        )

    status = snapshot_status(ImageStatus(), workspace)
    assert status.state == "failed"
    assert "did not finish" in status.detail


def test_a_second_build_is_refused_while_one_runs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeBuild(monkeypatch)
    directory = snapshots_dir(workspace)
    directory.mkdir(parents=True)

    with held_lock(directory / "build.lock"):
        with pytest.raises(
            AgentEnvironmentError,
            match=re.escape(
                t("intelligences.agent_environment.snapshot.build_running")
            ),
        ):
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


def test_the_service_says_once_that_the_declared_image_is_not_here(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeBuild(monkeypatch)
    monkeypatch.setattr(runtime, "doctor", lambda: AgentEnvironmentHealth(True))
    _device_holds(monkeypatch, {})
    monkeypatch.setattr(snapshot, "load_toolchain", _with_image)
    upkeep = _upkeep(monkeypatch, caplog)

    upkeep.once()
    upkeep.once()

    assert fake.calls == []
    assert caplog.text.count(image_load_command()) == 1


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
    assert isinstance(build_steps()[0], BuildStep)
