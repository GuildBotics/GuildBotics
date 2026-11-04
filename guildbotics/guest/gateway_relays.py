"""VM-lifetime raw TLS forwarding for providers with a fixed hostname."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

RELAY_ENV = "GUILDBOTICS_GATEWAY_RELAYS"
GUEST_HOST = "host.microsandbox.internal"
RELAY_ADDRESS = "127.0.0.2"
TLS_PORT = 443
CHUNK = 65536
PORT_LIMIT = 65536


async def forward(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int
) -> None:
    """Forward encrypted bytes without terminating TLS or parsing credentials."""
    upstream: asyncio.StreamWriter | None = None
    try:
        remote, upstream = await asyncio.open_connection(host, port)

        async def pump(
            source: asyncio.StreamReader, target: asyncio.StreamWriter
        ) -> None:
            while chunk := await source.read(CHUNK):
                target.write(chunk)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()

        await asyncio.gather(pump(reader, upstream), pump(remote, writer))
    finally:
        writer.close()
        if upstream is not None:
            upstream.close()
        await writer.wait_closed()
        if upstream is not None:
            await upstream.wait_closed()


@asynccontextmanager
async def relays(
    *,
    hosts_file: Path = Path("/etc/hosts"),
    listen_host: str = RELAY_ADDRESS,
    listen_port: int = TLS_PORT,
    target_host: str = GUEST_HOST,
    configuration: str | None = None,
) -> AsyncIterator[int | None]:
    """Start the host-approved relay before any command and stop it afterward."""
    data = json.loads(
        configuration if configuration is not None else os.environ.get(RELAY_ENV, "[]")
    )
    if not isinstance(data, list):
        raise ValueError("Invalid gateway relay configuration")
    if not data:
        yield None
        return
    ports = {entry["port"] for entry in data}
    if len(ports) != 1 or any(
        not isinstance(port, int) or isinstance(port, bool) or not 0 < port < PORT_LIMIT
        for port in ports
    ):
        raise ValueError("A VM relay requires one approved target port")
    names = {host for entry in data for host in entry["hosts"]}
    if not names or any(
        not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        for host in names
    ):
        raise ValueError("Invalid gateway relay hostname")
    port = next(iter(ports))
    tasks: set[asyncio.Task[None]] = set()

    def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(forward(reader, writer, target_host, port))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    original = hosts_file.read_bytes()
    server = await asyncio.start_server(connect, listen_host, listen_port)
    try:
        hosts_file.write_bytes(
            original
            + b"\n"
            + "".join(f"{listen_host} {host}\n" for host in sorted(names)).encode()
        )
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        hosts_file.write_bytes(original)


def trust_turn(
    turn: dict[str, Any],
    *,
    system_ca: Path = Path("/etc/ssl/certs/ca-certificates.crt"),
) -> dict[str, Any]:
    """Append the private gateway CA to system roots, never replace them."""
    env = dict(turn.get("env", {}))
    announced = env.get("NODE_EXTRA_CA_CERTS")
    if announced is None:
        return turn
    system = system_ca.read_bytes()
    authority = Path(announced).read_bytes()
    if not system.strip() or not authority.strip():
        raise ValueError("The system and gateway CA bundles must be non-empty")
    with tempfile.NamedTemporaryFile(
        prefix="guildbotics-ca-",
        suffix=".pem",
        dir=Path(announced).parent,
        delete=False,
    ) as bundle:
        bundle.write(system + b"\n" + authority)
        path = bundle.name
    env.update(SSL_CERT_FILE=path, REQUESTS_CA_BUNDLE=path)
    return {**turn, "env": env}
