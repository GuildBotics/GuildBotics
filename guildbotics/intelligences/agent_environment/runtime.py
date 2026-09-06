"""One microVM per turn, driven through the microsandbox SDK.

This is the only module that talks to the runtime. A :class:`AgentEnvironment` is
created from a snapshot for one turn -- boot from a snapshot takes a fraction
of a second -- with the mounts and network policy a :class:`AgentEnvironmentSpec`
states, runs the provider CLI inside with its stdio bridged to the host, and
is discarded when the turn ends. Cancelling a turn stops the microVM, so no
process survives it.

The SDK is imported when an environment is needed rather than when this module
is: a device without a wheel for its platform must still start GuildBotics
and be told, through :func:`doctor`, why no agent can run there.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from guildbotics.intelligences.agent_environment.spec import AgentEnvironmentSpec

#: How a sandbox GuildBotics created is named, so a stale one is recognisable.
_NAME_PREFIX = "guildbotics-"
#: Size of the empty mount that covers a denied directory.
_COVER_MIB = 1
#: The exit code reported when the guest process ended without one: the
#: runtime killed it, or its exec session broke.
_KILLED = -1
_STOP_TIMEOUT = 5.0


class AgentEnvironmentError(RuntimeError):
    """The environment could not be created or the runtime refused a step."""


@dataclass(frozen=True, slots=True)
class AgentEnvironmentHealth:
    """Whether this device can create an environment, and if not, why."""

    available: bool
    reason: str = ""
    runtime_version: str = ""


def doctor() -> AgentEnvironmentHealth:
    """Check that the SDK and its runtime are present on this device.

    Absent either, no agent may start here (fail-closed), and the reason is
    what the Desktop shows beside that refusal.
    """
    try:
        import microsandbox
    except ImportError:
        return AgentEnvironmentHealth(
            False, "The microsandbox SDK is not installed for this platform."
        )
    if not microsandbox.is_installed():
        return AgentEnvironmentHealth(
            False, "The microsandbox runtime (msb and libkrunfw) is not installed."
        )
    return AgentEnvironmentHealth(True, runtime_version=microsandbox.version())


class EnvironmentStdin:
    """The guest process' stdin, with the surface of an asyncio stream writer."""

    def __init__(self, sink: Any) -> None:
        self._sink = sink
        self._buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        data = bytes(self._buffer)
        self._buffer.clear()
        if not data:
            return
        try:
            await self._sink.write(data)
        except Exception as exc:
            raise ConnectionError(f"environment stdin closed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        asyncio.get_running_loop().create_task(self._aclose())

    def is_closing(self) -> bool:
        return self._closed

    async def _aclose(self) -> None:
        with suppress(Exception):
            await self._sink.close()


class EnvironmentProcess:
    """A process inside the environment, with the surface of an asyncio subprocess.

    ``stdout`` and ``stderr`` are stream readers fed from the runtime's event
    stream, so the line-oriented transports the adapters already use read
    them unchanged. ``returncode`` is set when the process ends; ``kill``
    ends it now.
    """

    def __init__(self, handle: Any, *, limit: int) -> None:
        self._handle = handle
        self.stdin = EnvironmentStdin(handle.take_stdin())
        self.stdout = asyncio.StreamReader(limit=limit)
        self.stderr = asyncio.StreamReader(limit=limit)
        self.pid: int | None = None
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self._pump = asyncio.create_task(self._pump_events())

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def kill(self) -> None:
        if self.returncode is None:
            with suppress(Exception):
                await self._handle.kill()
        await self.wait()

    async def _pump_events(self) -> None:
        try:
            async for event in self._handle:
                self._apply(event)
        except Exception as exc:  # the exec session itself broke
            if self.returncode is None:
                self.stderr.feed_data(f"{exc}\n".encode())
        finally:
            if self.returncode is None:
                self.returncode = _KILLED
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

    def _apply(self, event: Any) -> None:
        kind = str(event.event_type)
        if kind == "started":
            self.pid = event.pid
        elif kind == "stdout":
            self.stdout.feed_data(event.data or b"")
        elif kind == "stderr":
            self.stderr.feed_data(event.data or b"")
        elif kind == "exited":
            self.returncode = _KILLED if event.code is None else int(event.code)
        elif kind == "failed":
            # The runtime could not spawn the command; its reason is the
            # process' only output.
            self.stderr.feed_data((event.data or b"") + b"\n")
            self.returncode = int(event.code or 1)


class AgentEnvironment:
    """One turn's microVM."""

    def __init__(self, sandbox: Any, spec: AgentEnvironmentSpec) -> None:
        self._sandbox = sandbox
        self._closed = False
        self.spec = spec

    @classmethod
    async def start(
        cls, spec: AgentEnvironmentSpec, *, snapshot: str
    ) -> AgentEnvironment:
        """Boot a microVM from ``snapshot`` shaped by ``spec``.

        The sandbox is ephemeral: stopping it removes it, so nothing of a
        turn outlives the turn.
        """
        import microsandbox

        name = _NAME_PREFIX + secrets.token_hex(6)
        try:
            sandbox = await microsandbox.Sandbox.create(
                name,
                from_snapshot=snapshot,
                ephemeral=True,
                workdir=spec.cwd,
                volumes=_volumes(spec),
                network=_network(spec),
            )
        except Exception as exc:
            raise AgentEnvironmentError(
                f"Could not start the environment: {exc}"
            ) from exc
        return cls(sandbox, spec)

    async def run(self, command: str, *args: str, limit: int) -> EnvironmentProcess:
        """Start ``command`` inside the environment with its stdio bridged.

        Args:
            command: The program, resolved on the guest's PATH.
            args: Its arguments.
            limit: Buffer limit of the stdout / stderr readers; a transport
                that reads long single lines sets it high enough for them.
        """
        from microsandbox import Stdin

        try:
            handle = await self._sandbox.exec_stream(
                command,
                list(args),
                stdin=Stdin.pipe(),
                cwd=self.spec.cwd,
                env={"HOME": self.spec.home, **self.spec.env},
            )
        except Exception as exc:
            raise AgentEnvironmentError(
                f"Could not start '{command}' in the environment: {exc}"
            ) from exc
        return EnvironmentProcess(handle, limit=limit)

    async def close(self) -> None:
        """Stop the microVM; every process inside it ends with it."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._sandbox.stop(timeout=_STOP_TIMEOUT)
        except Exception:
            with suppress(Exception):
                await self._sandbox.destroy(force=True)


def _volumes(spec: AgentEnvironmentSpec) -> dict[str, Any]:
    from microsandbox import Volume

    return {
        mount.guest: (
            Volume.tmpfs(size_mib=_COVER_MIB, readonly=True)
            if mount.host is None
            else Volume.bind(str(mount.host), readonly=mount.readonly)
        )
        for mount in spec.mounts
    }


def _network(spec: AgentEnvironmentSpec) -> Any:
    from microsandbox import (
        Action,
        DestGroup,
        Destination,
        Network,
        NetworkPolicy,
        Protocol,
        Rule,
    )
    from microsandbox.types import DnsConfig

    network = spec.network
    rules: list[Any] = []
    if not network.unrestricted:
        rules.extend(Rule.allow_dns())
        rules.extend(
            Rule.allow(
                destination=Destination.group(DestGroup.HOST),
                protocol=Protocol.TCP,
                port=port,
            )
            for port in network.host_ports
        )
        rules.extend(
            Rule.allow(
                destination=(
                    Destination.domain_suffix(domain[2:])
                    if domain.startswith("*.")
                    else Destination.domain(domain)
                )
            )
            for domain in network.domains
        )
        if network.local_network:
            rules.extend(
                Rule.allow(destination=Destination.group(group))
                for group in (DestGroup.HOST, DestGroup.PRIVATE)
            )
    return Network(
        policy=NetworkPolicy(
            default_egress=Action.ALLOW if network.unrestricted else Action.DENY,
            default_ingress=Action.DENY,
            rules=tuple(rules),
        ),
        dns=DnsConfig(nameservers=network.nameservers),
    )
