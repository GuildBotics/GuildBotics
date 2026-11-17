"""Exercise gateway CA trust and failure cleanup without a microVM."""

from pathlib import Path

import pytest

from guildbotics.guest.gateway_relays import trust_turn


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
