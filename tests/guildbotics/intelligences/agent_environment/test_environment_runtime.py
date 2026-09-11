"""The runtime drives the SDK exactly as the spec says, and bridges stdio.

The SDK's own types are real; only the sandbox and its exec stream are
faked, so what is asserted is the configuration the runtime would hand to a
microVM and how it turns the runtime's events into a process.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import microsandbox
import pytest
from microsandbox import Action, DestGroup, MountKind, NetworkDestinationKind, Protocol

from guildbotics.intelligences.agent_environment import runtime
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
    AgentEnvironmentHealth,
    EnvironmentProcess,
    doctor,
)
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
)
from guildbotics.utils.i18n_tool import t


def _event(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{"event_type": kind, "pid": None, "data": None, "code": None, **fields}
    )


class _Sink:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.fail = False

    async def write(self, data: bytes) -> None:
        if self.fail:
            raise microsandbox.MicrosandboxError("stdin gone")
        self.written.append(data)

    async def close(self) -> None:
        self.closed = True


class _Handle:
    """An exec stream that replays scripted events; ``kill`` ends it."""

    def __init__(self, events: list[Any], *, gate: asyncio.Event | None = None) -> None:
        self._events = list(events)
        self._gate = gate
        self.sink = _Sink()
        self.killed = False

    def take_stdin(self) -> _Sink:
        return self.sink

    async def kill(self) -> None:
        """End the stream the way the runtime does: an exit event, then EOF."""
        self.killed = True
        self._events.append(_event("exited", code=None))
        gate, self._gate = self._gate, None
        if gate is not None:
            gate.set()

    def __aiter__(self) -> _Handle:
        return self

    async def __anext__(self) -> Any:
        while not self._events:
            if self._gate is None:
                raise StopAsyncIteration
            self._gate.clear()
            await self._gate.wait()
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class _Sandbox:
    created: dict[str, Any] = {}
    instance: _Sandbox | None = None

    def __init__(self) -> None:
        self.execs: list[dict[str, Any]] = []
        self.stopped = False
        self.destroyed = False
        self.handle = _Handle([])
        self.stop_error: Exception | None = None

    @classmethod
    async def create(cls, name: str, **kwargs: Any) -> _Sandbox:
        if name.startswith("fail"):
            raise microsandbox.MicrosandboxError("no hypervisor")
        cls.created = {"name": name, **kwargs}
        cls.instance = cls()
        return cls.instance

    async def exec_stream(self, cmd: str, args: list[str], **kwargs: Any) -> _Handle:
        if cmd == "explode":
            raise microsandbox.MicrosandboxError("agent unreachable")
        self.execs.append({"cmd": cmd, "args": args, **kwargs})
        return self.handle

    async def stop(self, timeout: float | None = None) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped = True

    async def destroy(
        self, *, force: bool = False, timeout: float | None = None
    ) -> None:
        self.destroyed = True


@pytest.fixture
def sandbox(monkeypatch) -> type[_Sandbox]:
    _Sandbox.created = {}
    _Sandbox.instance = None
    monkeypatch.setattr(microsandbox, "Sandbox", _Sandbox)
    return _Sandbox


def _spec(**overrides: Any) -> AgentEnvironmentSpec:
    network = EnvironmentNetwork(
        unrestricted=False,
        domains=("api.openai.com", "*.example.com"),
        host_ports=(43123,),
        local_network=False,
        nameservers=("1.1.1.1",),
    )
    fields: dict[str, Any] = {
        "cwd": "/work/repo",
        "home": "/home/u",
        "mounts": (
            EnvironmentMount("/work/repo", Path("/work/repo"), readonly=False),
            EnvironmentMount(
                "/home/u/Documents", Path("/home/u/Documents"), readonly=True
            ),
            EnvironmentMount("/work/repo/private", None, readonly=True),
        ),
        "network": network,
        "env": {"GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"},
    }
    fields.update(overrides)
    return AgentEnvironmentSpec(**fields)


# --- doctor ---------------------------------------------------------------------


@pytest.fixture
def bundled(monkeypatch, tmp_path: Path) -> Path:
    """A stand-in for the wheel's ``microsandbox/_bundled``: msb and libkrunfw."""
    root = tmp_path / "wheel" / "_bundled"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "bin" / runtime.runtime_binary(Path("x")).name).write_bytes(b"#!msb")
    (root / "lib" / "libkrunfw.5.dylib").write_bytes(b"krun")
    monkeypatch.setattr(runtime, "files", lambda package: root)
    monkeypatch.setattr(microsandbox, "version", lambda: "0.6.17")
    monkeypatch.delenv(runtime.RUNTIME_BINARY_ENV, raising=False)
    return root


def test_doctor_places_the_bundled_runtime_at_the_fixed_home_and_points_the_sdk_at_it(
    monkeypatch, bundled: Path
) -> None:
    monkeypatch.setattr(microsandbox, "is_installed", lambda: True)
    home = runtime.runtime_home()

    health = doctor()

    assert health == AgentEnvironmentHealth(
        True, runtime_version="0.6.17", home=str(home)
    )
    assert home == Path.home() / ".guildbotics" / "data" / "msb"
    binary = runtime.runtime_binary(home)
    assert binary.read_bytes() == b"#!msb"
    assert binary.stat().st_mode & 0o111
    assert (home / "lib" / "libkrunfw.5.dylib").read_bytes() == b"krun"
    assert (home / "version").read_text() == "0.6.17"
    assert os.environ[runtime.RUNTIME_HOME_ENV] == str(home)
    assert os.environ[runtime.RUNTIME_BINARY_ENV] == str(binary)


def test_doctor_copies_the_runtime_again_only_when_the_sdk_version_changed(
    monkeypatch, bundled: Path
) -> None:
    monkeypatch.setattr(microsandbox, "is_installed", lambda: True)
    home = runtime.runtime_home()
    doctor()
    (bundled / "bin" / runtime.runtime_binary(home).name).write_bytes(b"#!newer")

    doctor()
    assert runtime.runtime_binary(home).read_bytes() == b"#!msb"

    monkeypatch.setattr(microsandbox, "version", lambda: "0.7.0")
    doctor()
    assert runtime.runtime_binary(home).read_bytes() == b"#!newer"
    assert (home / "version").read_text() == "0.7.0"


def test_doctor_reports_a_runtime_the_sdk_still_does_not_see(
    monkeypatch, bundled: Path
) -> None:
    monkeypatch.setattr(microsandbox, "is_installed", lambda: False)
    assert doctor() == AgentEnvironmentHealth(
        False,
        t("intelligences.agent_environment.runtime.not_installed"),
        home=str(runtime.runtime_home()),
    )


def test_doctor_reports_a_home_the_runtime_cannot_be_placed_in(
    monkeypatch, bundled: Path
) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(runtime.shutil, "copyfile", refuse)

    health = doctor()

    assert not health.available
    assert health.reason == t(
        "intelligences.agent_environment.runtime.not_placed",
        home=runtime.runtime_home(),
        error="read-only",
    )


def test_the_windows_firewall_rule_is_created_once_for_the_fixed_path(
    monkeypatch, bundled: Path
) -> None:
    monkeypatch.setattr(microsandbox, "is_installed", lambda: True)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    calls: list[list[str]] = []
    missing = {"rule": True}

    def fake_run(command: list[str], **_: Any) -> SimpleNamespace:
        calls.append(command)
        if command[:2] == ["netsh", "advfirewall"]:
            return SimpleNamespace(returncode=1 if missing["rule"] else 0)
        missing["rule"] = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    doctor()
    # Shown once, missing, so created through an elevated netsh naming the
    # fixed binary; on the next placement the rule is there and nothing runs.
    assert [c[0] for c in calls] == ["netsh", "powershell"]
    assert runtime.FIREWALL_RULE_NAME in calls[0][-1]
    assert str(runtime.runtime_binary(runtime.runtime_home())) in calls[1][-1]
    assert "RunAs" in calls[1][-1]
    monkeypatch.setattr(microsandbox, "version", lambda: "0.7.0")
    doctor()
    assert [c[0] for c in calls] == ["netsh", "powershell", "netsh"]


def test_doctor_survives_a_platform_without_the_sdk(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "microsandbox":
            raise ImportError("no wheel")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    assert doctor() == AgentEnvironmentHealth(
        False, t("intelligences.agent_environment.runtime.sdk_missing")
    )


# --- start ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_boots_an_ephemeral_sandbox_from_the_snapshot_with_the_spec(
    sandbox: type[_Sandbox],
) -> None:
    boundary = await AgentEnvironment.start(_spec(), snapshot="guildbotics-toolchain")

    created = sandbox.created
    assert created["name"].startswith("guildbotics-")
    assert created["from_snapshot"] == "guildbotics-toolchain"
    assert created["ephemeral"] is True
    assert created["workdir"] == "/work/repo"
    volumes = created["volumes"]
    assert list(volumes) == ["/work/repo", "/home/u/Documents", "/work/repo/private"]
    assert (volumes["/work/repo"].kind, volumes["/work/repo"].bind) == (
        MountKind.BIND,
        "/work/repo",
    )
    assert volumes["/work/repo"].readonly is False
    assert volumes["/home/u/Documents"].readonly is True
    cover = volumes["/work/repo/private"]
    assert (cover.kind, cover.readonly) == (MountKind.TMPFS, True)
    assert boundary.spec is not None


@pytest.mark.asyncio
async def test_start_binds_the_host_side_of_a_mount_by_its_resolved_path(
    sandbox: type[_Sandbox], tmp_path: Path
) -> None:
    # macOS spells temporary directories under /var, a symlink to
    # /private/var, and the runtime cannot bind through the link. The guest
    # keeps the spelling it was given.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    spec = _spec(mounts=(EnvironmentMount("/work/link", link, readonly=False),))

    await AgentEnvironment.start(spec, snapshot="s")

    volume = sandbox.created["volumes"]["/work/link"]
    assert (volume.kind, volume.bind) == (MountKind.BIND, str(real.resolve()))


@pytest.mark.asyncio
async def test_a_closed_network_allows_only_dns_domains_and_host_ports(
    sandbox: type[_Sandbox],
) -> None:
    await AgentEnvironment.start(_spec(), snapshot="s")

    network = sandbox.created["network"]
    policy = network.policy
    assert (policy.default_egress, policy.default_ingress) == (Action.DENY, Action.DENY)
    assert network.dns.nameservers == ("1.1.1.1",)
    rules = [
        (r.action, r.destination.kind, r.destination.value, r.protocol, r.port)
        for r in policy.rules
    ]
    assert rules == [
        (Action.ALLOW, NetworkDestinationKind.GROUP, "host", Protocol.UDP, 53),
        (Action.ALLOW, NetworkDestinationKind.GROUP, "host", Protocol.TCP, 53),
        (Action.ALLOW, NetworkDestinationKind.GROUP, "host", Protocol.TCP, 43123),
        (Action.ALLOW, NetworkDestinationKind.DOMAIN, "api.openai.com", None, None),
        (Action.ALLOW, NetworkDestinationKind.DOMAIN_SUFFIX, "example.com", None, None),
    ]


@pytest.mark.asyncio
async def test_local_network_opens_the_host_and_private_ranges(
    sandbox: type[_Sandbox],
) -> None:
    network = EnvironmentNetwork(False, (), (), local_network=True, nameservers=())
    await AgentEnvironment.start(_spec(network=network), snapshot="s")

    groups = [
        r.destination.value
        for r in sandbox.created["network"].policy.rules
        if r.destination.kind == NetworkDestinationKind.GROUP and r.port is None
    ]
    assert groups == [DestGroup.HOST, DestGroup.PRIVATE]


@pytest.mark.asyncio
async def test_an_unrestricted_network_allows_all_egress_and_no_ingress(
    sandbox: type[_Sandbox],
) -> None:
    network = EnvironmentNetwork(
        True, (), (43123,), local_network=False, nameservers=()
    )
    await AgentEnvironment.start(_spec(network=network), snapshot="s")

    policy = sandbox.created["network"].policy
    assert (policy.default_egress, policy.default_ingress) == (
        Action.ALLOW,
        Action.DENY,
    )
    assert policy.rules == ()


@pytest.mark.asyncio
async def test_a_runtime_refusal_becomes_a_boundary_error(
    sandbox: type[_Sandbox], monkeypatch
) -> None:
    monkeypatch.setattr(runtime.secrets, "token_hex", lambda n: "x")
    monkeypatch.setattr(runtime, "_NAME_PREFIX", "fail-")

    with pytest.raises(AgentEnvironmentError, match="no hypervisor"):
        await AgentEnvironment.start(_spec(), snapshot="s")


# --- run ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_starts_the_command_in_the_cwd_with_the_spec_environment(
    sandbox: type[_Sandbox],
) -> None:
    boundary = await AgentEnvironment.start(_spec(), snapshot="s")

    await boundary.run("codex", "app-server", "-c", "x=1", limit=1024)

    (call,) = sandbox.instance.execs  # type: ignore[union-attr]
    assert (call["cmd"], call["args"]) == ("codex", ["app-server", "-c", "x=1"])
    assert call["cwd"] == "/work/repo"
    # The guest's home is the host's, so the provider keeps its state where
    # the snapshot put it.
    assert call["env"] == {"HOME": "/home/u", "GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"}
    assert call["stdin"] == microsandbox.Stdin.pipe()

    with pytest.raises(AgentEnvironmentError, match="agent unreachable"):
        await boundary.run("explode", limit=1024)


@pytest.mark.asyncio
async def test_a_process_bridges_stdio_and_reports_the_exit_code() -> None:
    handle = _Handle(
        [
            _event("started", pid=207),
            _event("stdout", data=b'{"id":1}\n{"id":'),
            _event("stderr", data=b"warn"),
            _event("stdout", data=b"2}\n"),
            _event("stderr", data=b"ing\n"),
            _event("exited", code=3),
        ]
    )
    process = EnvironmentProcess(handle, limit=1 << 16)

    process.stdin.write(b'{"method":"initialize"}\n')
    await process.stdin.drain()
    process.stdin.close()
    lines = [await process.stdout.readline(), await process.stdout.readline()]
    assert lines == [b'{"id":1}\n', b'{"id":2}\n']
    assert await process.stdout.readline() == b""
    assert await process.stderr.read() == b"warning\n"
    assert await process.wait() == 3
    assert (process.returncode, process.pid) == (3, 207)
    await asyncio.sleep(0)
    assert handle.sink.written == [b'{"method":"initialize"}\n']
    assert handle.sink.closed
    assert process.stdin.is_closing()


@pytest.mark.asyncio
async def test_a_process_the_runtime_could_not_spawn_fails_with_the_reason() -> None:
    handle = _Handle([_event("failed", data=b'spawn "codex": No such file', code=2)])
    process = EnvironmentProcess(handle, limit=1 << 16)

    assert await process.wait() == 2
    assert await process.stderr.read() == b'spawn "codex": No such file\n'


@pytest.mark.asyncio
async def test_a_broken_exec_session_ends_the_process_as_killed() -> None:
    handle = _Handle([_event("stdout", data=b"partial"), RuntimeError("session lost")])
    process = EnvironmentProcess(handle, limit=1 << 16)

    assert await process.wait() == -1
    assert await process.stdout.read() == b"partial"
    assert await process.stderr.read() == b"session lost\n"


@pytest.mark.asyncio
async def test_kill_ends_a_running_process_and_a_closed_stdin_raises() -> None:
    handle = _Handle([_event("started", pid=1)], gate=asyncio.Event())
    process = EnvironmentProcess(handle, limit=1 << 16)
    await asyncio.sleep(0)
    assert process.returncode is None

    await process.kill()

    assert handle.killed
    assert process.returncode == -1
    handle.sink.fail = True
    process.stdin.write(b"x")
    with pytest.raises(ConnectionError):
        await process.stdin.drain()


# --- close ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_stops_the_sandbox_once_and_destroys_it_when_stopping_fails(
    sandbox: type[_Sandbox],
) -> None:
    boundary = await AgentEnvironment.start(_spec(), snapshot="s")
    instance = sandbox.instance
    assert instance is not None

    await boundary.close()
    await boundary.close()
    assert instance.stopped and not instance.destroyed

    stuck = await AgentEnvironment.start(_spec(), snapshot="s")
    assert sandbox.instance is not None
    sandbox.instance.stop_error = microsandbox.MicrosandboxError("stuck")
    await stuck.close()
    assert sandbox.instance.destroyed
