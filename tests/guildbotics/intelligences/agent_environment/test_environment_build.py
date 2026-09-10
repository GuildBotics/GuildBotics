"""Building a snapshot drives the SDK as the recipe says, and cleans up after itself."""

from __future__ import annotations

import re
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import microsandbox
import pytest
from microsandbox import Action

from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    BuildStep,
    build_snapshot,
    remove_snapshot,
)
from guildbotics.utils.i18n_tool import t


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

    @classmethod
    async def create(cls, name: str, **kwargs: Any) -> _Sandbox:
        if cls.create_error is not None:
            raise cls.create_error
        cls.created = {"name": name, **kwargs}
        cls.docker_config = os.environ.get("DOCKER_CONFIG")
        return cls()

    @staticmethod
    async def get(name: str) -> _Sandbox:
        return _Sandbox()

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


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    _Sandbox.created, _Sandbox.execs = {}, []
    _Sandbox.stopped = _Sandbox.destroyed = False
    _Sandbox.create_error = None
    _Snapshot.created, _Snapshot.removed = {}, []
    monkeypatch.setattr(microsandbox, "Sandbox", _Sandbox)
    monkeypatch.setattr(microsandbox, "Snapshot", _Snapshot)


_STEPS = (BuildStep("home", 'mkdir -p "$HOME"'), BuildStep("npm", "npm install -g x"))


@pytest.mark.asyncio
async def test_a_build_runs_every_step_with_the_home_and_keeps_the_result(
    sdk: None, tmp_path: Path
) -> None:
    lines: list[str] = []

    path = await build_snapshot(
        "guildbotics-abc",
        dest_dir=tmp_path,
        image="node:22.23.2-bookworm",
        home="/Users/u",
        steps=_STEPS,
        nameservers=("10.0.0.53",),
        on_line=lines.append,
    )

    created = _Sandbox.created
    assert created["name"] == "guildbotics-build"
    assert created["image"] == "node:22.23.2-bookworm"
    assert created["replace"] is True
    assert "volumes" not in created
    policy = created["network"].policy
    assert (policy.default_egress, policy.default_ingress) == (
        Action.ALLOW,
        Action.DENY,
    )
    assert policy.rules == ()
    assert created["network"].dns.nameservers == ("10.0.0.53",)
    assert [e["args"] for e in _Sandbox.execs] == [
        ["-ec", 'mkdir -p "$HOME"'],
        ["-ec", "npm install -g x"],
    ]
    assert all(e["cmd"] == "sh" for e in _Sandbox.execs)
    assert _Sandbox.execs[0]["env"] == {
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


@pytest.mark.asyncio
async def test_the_pull_ignores_the_docker_clients_configuration(
    sdk: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker Desktop's credential helper can block for good; the base image
    needs no credentials, so the pull sees an empty configuration instead."""
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker"))

    await build_snapshot(
        "n",
        dest_dir=tmp_path,
        image="i",
        home="/h",
        steps=(),
        nameservers=(),
        on_line=lambda _: None,
    )

    assert _Sandbox.docker_config is not None
    assert _Sandbox.docker_config != str(tmp_path / "docker")
    assert not Path(_Sandbox.docker_config).exists()
    assert os.environ["DOCKER_CONFIG"] == str(tmp_path / "docker")


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
            "guildbotics-abc",
            dest_dir=tmp_path,
            image="i",
            home="/h",
            steps=(BuildStep("apt", "apt-get install fail"), _STEPS[1]),
            nameservers=(),
            on_line=lines.append,
        )

    assert lines == ["[apt]", "boom"]
    assert len(_Sandbox.execs) == 1
    assert _Snapshot.created == {}
    assert _Sandbox.destroyed


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_start_is_reported(
    sdk: None, tmp_path: Path
) -> None:
    _Sandbox.create_error = microsandbox.MicrosandboxError("no hypervisor")

    with pytest.raises(AgentEnvironmentError, match="build environment: no hypervisor"):
        await build_snapshot(
            "n",
            dest_dir=tmp_path,
            image="i",
            home="/h",
            steps=_STEPS,
            nameservers=(),
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
