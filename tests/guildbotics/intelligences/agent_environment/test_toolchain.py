"""The toolchain declaration: parsed strictly, read with the template fallback."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from guildbotics.intelligences.agent_environment import toolchain
from guildbotics.intelligences.agent_environment.contract import NetworkPolicy
from guildbotics.intelligences.agent_environment.toolchain import (
    TOOLCHAIN_PATH,
    DnsSettings,
    ToolchainError,
    device_nameservers,
    load_toolchain,
    parse_toolchain,
    upstream_nameservers,
)
from guildbotics.utils.fileio import get_template_path, load_yaml_file
from guildbotics.utils.i18n_tool import t

_DNS = {"nameservers": ["10.0.0.53"]}


def test_a_full_declaration_parses() -> None:
    declaration = parse_toolchain(
        {
            "image": {
                "reference": "local/agent:1",
                "digests": {
                    "arm64": "sha256:" + "c" * 64,
                    "amd64": "sha256:" + "d" * 64,
                },
            },
            "network": {
                "mode": "allowlist",
                "allowed_domains": ["github.com", "pypi.org"],
                "allow_local_network": False,
            },
            "dns": {"nameservers": ["10.0.0.53", "1.1.1.1"]},
        },
        where="test",
    )

    assert declaration.image is not None
    assert declaration.image.reference == "local/agent:1"
    assert declaration.image.digest_for("arm64") == "sha256:" + "c" * 64
    assert declaration.image.digest_for("amd64") == "sha256:" + "d" * 64
    assert declaration.image.digest_for("riscv64") == ""
    assert declaration.network.mode == "allowlist"
    assert declaration.network.allowed_domains == ["github.com", "pypi.org"]
    assert declaration.dns.nameservers == ["10.0.0.53", "1.1.1.1"]


def test_network_defaults_to_deny_but_dns_is_required() -> None:
    assert parse_toolchain({"dns": _DNS}, where="t").network == NetworkPolicy()
    assert parse_toolchain({"dns": _DNS}, where="t").image is None
    with pytest.raises(ToolchainError, match="dns"):
        parse_toolchain({"network": {}}, where="t")


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "a", "mapping"],
        {"dns": _DNS, "image": "node:22"},
        {"dns": _DNS, "image": {"reference": "node:22"}},
        {"dns": _DNS, "image": {"reference": "a:1", "digest": "sha256:" + "0" * 64}},
        {"dns": _DNS, "image": {"reference": "a:1", "digests": {}}},
        {
            "dns": _DNS,
            "image": {"reference": "a:1", "digests": {"arm64": "sha256:abc"}},
        },
        {
            "dns": _DNS,
            "image": {"reference": "a:1", "digests": {"ARM 64": "sha256:" + "0" * 64}},
        },
        {
            "dns": _DNS,
            "image": {"reference": "a b", "digests": {"arm64": "sha256:" + "0" * 64}},
        },
        {
            "dns": _DNS,
            "image": {"reference": "-x", "digests": {"arm64": "sha256:" + "0" * 64}},
        },
        {
            "dns": _DNS,
            "image": {"reference": "", "digests": {"arm64": "sha256:" + "0" * 64}},
        },
        {"dns": _DNS, "packages": {}},
        {"dns": _DNS, "network": "deny"},
        {"dns": _DNS, "network": {"mode": "allowlist"}},
        {
            "dns": _DNS,
            "network": {"mode": "deny", "allowed_domains": ["github.com"]},
        },
        {"dns": {"nameservers": []}},
        {"dns": {"nameservers": ["one.one.one.one"]}},
        {"dns": {"nameservers": "system"}},
        {"dns": {"nameservers": ["host"]}},
        {"dns": {"nameservers": ["2606:4700:4700::1111"]}},
        {"dns": {"nameservers": ["1.1.1.1"], "search": ["corp"]}},
    ],
)
def test_anything_the_declaration_does_not_define_is_rejected(raw: object) -> None:
    """An image is a reference and per-architecture digest; unknown fields,
    inconsistent network rules, IPv6, and hostname resolvers have no meaning."""
    with pytest.raises(ToolchainError, match="^test: "):
        parse_toolchain(raw, where="test")


def test_the_template_is_a_valid_declaration() -> None:
    template = get_template_path() / TOOLCHAIN_PATH

    declaration = parse_toolchain(load_yaml_file(template), where="template")

    assert declaration.network.mode == "allowlist"
    assert "api.github.com" in declaration.network.allowed_domains
    assert declaration.dns.nameservers == ["1.1.1.1", "8.8.8.8"]


def test_the_workspace_copy_wins_over_the_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".guildbotics" / "config"
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    assert load_toolchain().network.mode == "allowlist"

    target = config / TOOLCHAIN_PATH
    target.parent.mkdir(parents=True)
    target.write_text(
        "network:\n  mode: unrestricted\n  allowed_domains: []\n"
        "  allow_local_network: false\ndns:\n  nameservers: [10.0.0.53]\n"
    )

    assert load_toolchain().network.mode == "unrestricted"
    assert load_toolchain().dns.nameservers == ["10.0.0.53"]


# --- resolvers ---------------------------------------------------------------------


def test_host_names_the_devices_resolvers_and_a_list_stands_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(toolchain, "device_nameservers", lambda: ("192.168.3.1",))

    assert upstream_nameservers(DnsSettings(nameservers="host")) == ("192.168.3.1",)
    assert upstream_nameservers(DnsSettings(nameservers=["10.0.0.53"])) == (
        "10.0.0.53",
    )


def test_host_without_an_ipv4_resolver_is_an_error_naming_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(toolchain, "device_nameservers", lambda: ())

    expected = t(
        "intelligences.agent_environment.declaration.no_device_resolver",
        where=toolchain.TOOLCHAIN_PATH,
        host="host",
    )
    with pytest.raises(ToolchainError, match=re.escape(expected)):
        upstream_nameservers(DnsSettings(nameservers="host"))


def test_resolv_conf_yields_the_ipv4_resolvers_in_order_without_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway speaks IPv4 only, so an IPv6 upstream -- which is exactly
    what made the gateway's own default fail Codex -- is left out."""
    conf = tmp_path / "resolv.conf"
    conf.write_text(
        "# generated\nsearch corp.example\n"
        "nameserver 2400:2653:4280:2200:1111:1111:1111:1111\n"
        "nameserver 192.168.3.1\nnameserver 1.1.1.1\nnameserver 192.168.3.1\n"
        "nameserver\noptions ndots:1\n"
    )
    monkeypatch.setattr(toolchain, "_RESOLV_CONF", conf)
    monkeypatch.setattr(toolchain.sys, "platform", "darwin")

    assert device_nameservers() == ("192.168.3.1", "1.1.1.1")

    monkeypatch.setattr(toolchain, "_RESOLV_CONF", tmp_path / "absent")
    assert device_nameservers() == ()


def test_windows_asks_powershell_for_the_ipv4_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv)
        return type(
            "Done", (), {"stdout": "10.0.0.53\r\n10.0.0.54\r\n", "returncode": 0}
        )()

    monkeypatch.setattr(toolchain.sys, "platform", "win32")
    monkeypatch.setattr(toolchain.subprocess, "run", run)

    assert device_nameservers() == ("10.0.0.53", "10.0.0.54")
    assert calls[0][0] == "powershell" and "IPv4" in calls[0][-1]
