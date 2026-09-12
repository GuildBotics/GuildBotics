from __future__ import annotations

import asyncio
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment.runtime import AgentEnvironmentError

from guildbotics.intelligences.agent_runtime import (
    acp,
    antigravity,
    claude,
    codex,
    usage,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
)


@pytest.fixture(autouse=True)
def _adapter_member_broker_without_socket(request, monkeypatch) -> None:
    """Keep adapter tests local; the broker module owns real HTTP coverage."""
    if request.module.__name__.endswith("test_member_broker"):
        return

    async def start(broker: MemberCapabilityBroker) -> None:
        broker._url = "http://127.0.0.1:43123/mcp"
        broker._port = 43123

    monkeypatch.setattr(MemberCapabilityBroker, "_start", start)


class FakeEnvironment:
    """Stands in for the agent environment: what the adapter asked to boot,
    and the process the test scripted, started through ``asyncio``'s
    ``create_subprocess_exec`` so a test's own fake process is what runs."""

    started: list[FakeEnvironment] = []

    def __init__(self, tool: str, kwargs: dict[str, Any]) -> None:
        self.tool = tool
        self.kwargs = kwargs
        self.commands: list[tuple[str, ...]] = []
        self.closed = False
        FakeEnvironment.started.append(self)

    @property
    def spec(self) -> Any:
        context = self.kwargs.get("context")
        home = "/home/member"
        cwd = str(context.cwd) if context is not None else home
        return type("Spec", (), {"cwd": cwd, "home": home, "mounts": ()})()

    async def run(self, command: str, *args: str, limit: int) -> Any:
        self.commands.append((command, *args))
        context = self.kwargs.get("context")
        try:
            process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=limit,
                cwd=str(context.cwd) if context is not None else self.spec.home,
                env=dict(self.kwargs.get("env", {})),
            )
        except OSError as exc:
            raise AgentEnvironmentError(str(exc)) from exc
        return _ProcessInEnvironment(process)

    async def close(self) -> None:
        self.closed = True


class _Stdin:
    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _ProcessInEnvironment:
    """A test's fake process with the surface of an environment process:
    ``kill`` is awaited, ``stdin`` always exists, ``communicate`` reads."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        if getattr(inner, "stdin", None) is None:
            inner.stdin = _Stdin()
        if getattr(inner, "stderr", None) is None:
            inner.stderr = asyncio.StreamReader()
            inner.stderr.feed_eof()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_inner":
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    async def kill(self) -> None:
        inner = self._inner
        killer = getattr(inner, "kill", None)
        if killer is not None:
            result = killer()
            if asyncio.iscoroutine(result):
                await result
        if inner.returncode is None:
            inner.returncode = -1

    async def communicate(self) -> tuple[bytes, bytes]:
        inner = self._inner
        if hasattr(inner, "communicate"):
            return await inner.communicate()
        inner.stdin.close()
        stdout, stderr = await asyncio.gather(inner.stdout.read(), inner.stderr.read())
        await inner.wait()
        return stdout, stderr


@pytest.fixture(autouse=True)
def fake_environment(request, monkeypatch) -> type[FakeEnvironment]:
    """Every adapter boots a fake environment unless a test says otherwise."""
    FakeEnvironment.started = []
    if request.module.__name__.endswith("test_member_broker"):
        return FakeEnvironment

    async def start_turn(context: Any, tool: str, **kwargs: Any) -> FakeEnvironment:
        return FakeEnvironment(tool, {"context": context, **kwargs})

    async def start_probe(tool: str) -> FakeEnvironment:
        return FakeEnvironment(tool, {})

    for module in (codex, acp, claude, antigravity):
        monkeypatch.setattr(module, "start_turn_environment", start_turn)
    for module in (usage, antigravity):
        monkeypatch.setattr(module, "start_probe_environment", start_probe)
    return FakeEnvironment
