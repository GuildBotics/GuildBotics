"""Fake loopback servers exercise relay and CA lifetime without a microVM."""

import asyncio
import json
from pathlib import Path

import pytest

from guildbotics.guest.gateway_relays import relays, trust_turn


@pytest.mark.asyncio
async def test_raw_relay_starts_before_use_and_restores_hosts(tmp_path):
    async def echo(reader, writer):
        data = await reader.read(64)
        writer.write(data)
        await writer.drain()
        writer.close()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = target.sockets[0].getsockname()[1]
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    config = json.dumps([{"hosts": ["www.googleapis.com"], "port": port}])
    async with relays(
        hosts_file=hosts,
        listen_host="127.0.0.1",
        listen_port=0,
        target_host="127.0.0.1",
        configuration=config,
    ) as relay_port:
        assert "127.0.0.1 www.googleapis.com" in hosts.read_text()
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"TLS-shaped bytes")
        await writer.drain()
        assert await reader.read(64) == b"TLS-shaped bytes"
        writer.close()
        await writer.wait_closed()
    assert hosts.read_text() == "127.0.0.1 localhost\n"
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", relay_port)
    target.close()
    await target.wait_closed()


def test_ca_bundle_adds_gateway_ca_without_discarding_system_roots(tmp_path):
    system = tmp_path / "system.pem"
    system.write_text("system root\n")
    announced = tmp_path / "gateway.pem"
    announced.write_text("gateway root\n")
    turn = {
        "env": {"NODE_EXTRA_CA_CERTS": str(announced), "SSL_CERT_FILE": str(announced)}
    }
    fixed = trust_turn(turn, system_ca=system)
    env = fixed["env"]
    assert env["NODE_EXTRA_CA_CERTS"] == str(announced)
    assert env["SSL_CERT_FILE"] == env["REQUESTS_CA_BUNDLE"]
    assert Path(env["SSL_CERT_FILE"]).read_text() == "system root\n\ngateway root\n"
    with pytest.raises(FileNotFoundError):
        trust_turn(turn, system_ca=tmp_path / "missing")


@pytest.mark.asyncio
async def test_bad_relay_configuration_leaves_hosts_unchanged(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("original")
    with pytest.raises(ValueError):
        async with relays(
            hosts_file=hosts,
            configuration=json.dumps([{"hosts": ["host\ninjection"], "port": 443}]),
        ):
            pytest.fail("invalid relay admitted")
    assert hosts.read_text() == "original"


@pytest.mark.asyncio
async def test_ca_failure_releases_the_turn_grant(tmp_path, monkeypatch):
    from guildbotics.guest.host_client import HostClient

    calls = []

    async def call(self, name, **arguments):
        calls.append((name, arguments))
        if name == "begin_turn":
            return {
                "turn_grant": "fixture-grant",
                "env": {"NODE_EXTRA_CA_CERTS": str(tmp_path / "missing.pem")},
            }
        return {"refusal": ""}

    monkeypatch.setattr(HostClient, "acall", call)
    with pytest.raises(FileNotFoundError):
        await HostClient("http://window.test", "fixture").begin_turn("codex", "/work")
    assert calls[-1] == ("end_turn", {"turn_grant": "fixture-grant"})
