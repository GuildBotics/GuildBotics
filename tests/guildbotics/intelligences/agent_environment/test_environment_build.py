"""Building a snapshot drives the SDK as the recipe says, and cleans up after itself."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import microsandbox
import pytest
from microsandbox import Action

from guildbotics.intelligences.agent_environment import runtime
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    BuildStep,
    build_snapshot,
    remove_snapshot,
)
from guildbotics.utils.i18n_tool import t

_RESOURCES = {"memory_mib": 3072, "cpus": 3}


def _event(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{"event_type": kind, "pid": None, "data": None, "code": None, **fields}
    )


class _Handle:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def take_stdin(self) -> Any:
        return SimpleNamespace(write=None, close=None)

    def __aiter__(self) -> _Handle:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _Sandbox:
    """Records the build's steps; a step whose script says ``fail`` exits 7."""

    created: dict[str, Any] = {}
    execs: list[dict[str, Any]] = []
    stopped = False
    destroyed = False
    create_error: Exception | None = None
    docker_config: str | None = None
    #: The manifest the runtime resolved the reference to at creation.
    manifest_digest = "sha256:" + "m" * 64
    config_json = ""

    @classmethod
    async def create(cls, name: str, **kwargs: Any) -> _Sandbox:
        if cls.create_error is not None:
            raise cls.create_error
        cls.created = {"name": name, **kwargs}
        cls.docker_config = os.environ.get("DOCKER_CONFIG")
        return cls()

    @staticmethod
    async def get(name: str) -> _Sandbox:
        sandbox = _Sandbox()
        sandbox.config_json = json.dumps(
            {
                "image": {"Oci": {"reference": "x"}},
                "manifest_digest": _Sandbox.manifest_digest,
            }
        )
        return sandbox

    async def exec_stream(self, cmd: str, args: list[str], **kwargs: Any) -> _Handle:
        _Sandbox.execs.append({"cmd": cmd, "args": args, **kwargs})
        if "fail" in args[-1]:
            return _Handle([_event("stderr", data=b"boom\n"), _event("exited", code=7)])
        return _Handle([_event("stdout", data=b"done\n"), _event("exited", code=0)])

    async def stop(self, timeout: float | None = None) -> None:
        _Sandbox.stopped = True

    async def destroy(
        self, *, force: bool = False, timeout: float | None = None
    ) -> None:
        _Sandbox.destroyed = True


class _Snapshot:
    created: dict[str, Any] = {}
    removed: list[dict[str, Any]] = []

    @classmethod
    async def create(cls, name: str, **kwargs: Any) -> Any:
        cls.created = {"name": name, **kwargs}
        return SimpleNamespace(path=f"{kwargs['dest_dir']}/{name}")

    @classmethod
    async def remove(cls, path: str, *, force: bool = False) -> None:
        if path.endswith("locked"):
            raise microsandbox.MicrosandboxError("in use")
        cls.removed.append({"path": path, "force": force})


def _handle(reference: str, digest: str, manifest: str) -> Any:
    async def inspect() -> Any:
        return SimpleNamespace(config=SimpleNamespace(digest=digest))

    return SimpleNamespace(
        reference=reference,
        size_bytes=1,
        architecture="arm64",
        manifest_digest=manifest,
        inspect=inspect,
    )


class _Image:
    """The store: the built-from manifest belongs to ``local/agent:1``."""

    handles: list[Any] = []

    @staticmethod
    async def list() -> list[Any]:
        return list(_Image.handles)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    _Sandbox.created, _Sandbox.execs = {}, []
    _Sandbox.stopped = _Sandbox.destroyed = False
    _Sandbox.create_error = None
    _Sandbox.manifest_digest = "sha256:" + "m" * 64
    _Snapshot.created, _Snapshot.removed = {}, []
    _Image.handles = [
        _handle("node:22.23.2-bookworm", "sha256:" + "a" * 64, "sha256:" + "n" * 64),
        _handle("local/agent:1", "sha256:" + "c" * 64, "sha256:" + "m" * 64),
    ]
    monkeypatch.setattr(microsandbox, "Sandbox", _Sandbox)
    monkeypatch.setattr(microsandbox, "Snapshot", _Snapshot)
    monkeypatch.setattr(microsandbox, "Image", _Image)


def _named(name: str):
    """A naming callback that records what the runtime built from."""
    seen: list[str] = []

    def name_for(digest: str) -> str:
        seen.append(digest)
        return name

    name_for.seen = seen  # type: ignore[attr-defined]
    return name_for


_STEPS = (BuildStep("home", 'mkdir -p "$HOME"'), BuildStep("npm", "npm install -g x"))


@pytest.mark.asyncio
async def test_a_build_runs_every_step_with_the_home_and_keeps_the_result(
    sdk: None, tmp_path: Path
) -> None:
    lines: list[str] = []
    name = _named("guildbotics-abc")

    path = await build_snapshot(
        name,
        dest_dir=tmp_path,
        image="node:22.23.2-bookworm",
        pull=True,
        home="/Users/u",
        steps=_STEPS,
        nameservers=("10.0.0.53",),
        **_RESOURCES,
        on_line=lines.append,
    )

    created = _Sandbox.created
    assert created["name"] == "guildbotics-build"
    assert created["image"] == "node:22.23.2-bookworm"
    assert created["pull_policy"] == microsandbox.PullPolicy.IF_MISSING
    assert created["replace"] is True
    assert (created["memory"], created["cpus"]) == (3072, 3)
    assert "volumes" not in created
    policy = created["network"].policy
    assert (policy.default_egress, policy.default_ingress) == (
        Action.ALLOW,
        Action.DENY,
    )
    assert policy.rules == ()
    assert created["network"].dns.nameservers == ("10.0.0.53",)
    assert [e["args"] for e in _Sandbox.execs] == [
        ["-ec", runtime._IPV4_ONLY],
        ["-ec", 'mkdir -p "$HOME"'],
        ["-ec", "npm install -g x"],
    ]
    assert all(e["cmd"] == "sh" for e in _Sandbox.execs)
    assert _Sandbox.execs[1]["env"] == {
        "HOME": "/Users/u",
        "DEBIAN_FRONTEND": "noninteractive",
    }
    assert lines == ["[home]", "done", "[npm]", "done"]
    assert _Sandbox.stopped
    assert _Snapshot.created == {
        "name": "guildbotics-abc",
        "from_sandbox": "guildbotics-build",
        "dest_dir": str(tmp_path),
        "force": True,
    }
    assert path == tmp_path / "guildbotics-abc"
    assert _Sandbox.destroyed
    # The name came from the image the runtime resolved the reference to.
    assert name.seen == ["sha256:" + "c" * 64]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_build_from_an_image_the_store_cannot_name_reports_no_digest(
    sdk: None, tmp_path: Path
) -> None:
    _Sandbox.manifest_digest = "sha256:" + "z" * 64
    name = _named("n")

    await build_snapshot(
        name,
        dest_dir=tmp_path,
        image="i",
        pull=True,
        home="/h",
        steps=(),
        nameservers=(),
        **_RESOURCES,
        on_line=lambda _: None,
    )

    assert name.seen == [""]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_pull_ignores_the_docker_clients_configuration(
    sdk: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker Desktop's credential helper can block for good; the base image
    needs no credentials, so the pull sees an empty configuration instead."""
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker"))

    await build_snapshot(
        _named("n"),
        dest_dir=tmp_path,
        image="i",
        pull=True,
        home="/h",
        steps=(),
        nameservers=(),
        **_RESOURCES,
        on_line=lambda _: None,
    )

    assert _Sandbox.docker_config is not None
    assert _Sandbox.docker_config != str(tmp_path / "docker")
    assert not Path(_Sandbox.docker_config).exists()
    assert os.environ["DOCKER_CONFIG"] == str(tmp_path / "docker")


@pytest.mark.asyncio
async def test_an_image_loaded_here_is_never_asked_of_a_registry(
    sdk: None, tmp_path: Path
) -> None:
    """A local reference names nothing anywhere else; a pull would only
    fail slowly where refusing it fails at once."""
    await build_snapshot(
        _named("n"),
        dest_dir=tmp_path,
        image="local/agent:1",
        pull=False,
        home="/h",
        steps=(),
        nameservers=(),
        **_RESOURCES,
        on_line=lambda _: None,
    )

    assert _Sandbox.created["pull_policy"] == microsandbox.PullPolicy.NEVER


@pytest.mark.asyncio
async def test_a_failing_step_names_itself_and_the_build_sandbox_is_dropped(
    sdk: None, tmp_path: Path
) -> None:
    lines: list[str] = []

    failed = t(
        "intelligences.agent_environment.runtime.build_step_failed", step="apt", code=7
    )
    with pytest.raises(AgentEnvironmentError, match=re.escape(failed)):
        await build_snapshot(
            _named("guildbotics-abc"),
            dest_dir=tmp_path,
            image="i",
            pull=True,
            home="/h",
            steps=(BuildStep("apt", "apt-get install fail"), _STEPS[1]),
            nameservers=(),
            **_RESOURCES,
            on_line=lines.append,
        )

    assert lines == ["[apt]", "boom"]
    assert len(_Sandbox.execs) == 2
    assert _Snapshot.created == {}
    assert _Sandbox.destroyed


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_start_is_reported(
    sdk: None, tmp_path: Path
) -> None:
    _Sandbox.create_error = microsandbox.MicrosandboxError("no hypervisor")

    with pytest.raises(AgentEnvironmentError, match="build environment: no hypervisor"):
        await build_snapshot(
            _named("n"),
            dest_dir=tmp_path,
            image="i",
            pull=True,
            home="/h",
            steps=_STEPS,
            nameservers=(),
            **_RESOURCES,
            on_line=lambda _: None,
        )


@pytest.mark.asyncio
async def test_removing_a_snapshot_forgets_it_by_path(
    sdk: None, tmp_path: Path
) -> None:
    await remove_snapshot(tmp_path / "guildbotics-old")

    assert _Snapshot.removed == [
        {"path": str(tmp_path / "guildbotics-old"), "force": True}
    ]
    with pytest.raises(AgentEnvironmentError, match="in use"):
        await remove_snapshot(tmp_path / "locked")
