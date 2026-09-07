"""The toolchain declaration: parsed strictly, read with the template fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.intelligences.agent_environment import toolchain
from guildbotics.intelligences.agent_environment.toolchain import (
    TOOLCHAIN_PATH,
    DnsSettings,
    ToolchainDeclaration,
    ToolchainError,
    device_nameservers,
    load_toolchain,
    parse_toolchain,
    upstream_nameservers,
)
from guildbotics.utils.fileio import get_template_path, load_yaml_file

_DNS = {"nameservers": ["10.0.0.53"]}


def test_a_full_declaration_parses() -> None:
    declaration = parse_toolchain(
        {
            "packages": {
                "apt": ["ripgrep=14.1.0-1"],
                "npm": ["typescript@5.6.3"],
                "uv": ["ruff==0.6.9"],
            },
            "dns": {"nameservers": ["10.0.0.53", "1.1.1.1"]},
        },
        where="test",
    )

    assert declaration.packages.apt == ["ripgrep=14.1.0-1"]
    assert declaration.packages.npm == ["typescript@5.6.3"]
    assert declaration.packages.uv == ["ruff==0.6.9"]
    assert declaration.dns.nameservers == ["10.0.0.53", "1.1.1.1"]


def test_packages_default_to_nothing_but_dns_is_required() -> None:
    assert parse_toolchain({"dns": _DNS}, where="t").packages.apt == []
    with pytest.raises(ToolchainError, match="dns"):
        parse_toolchain({"packages": {}}, where="t")


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "a", "mapping"],
        {"dns": _DNS, "image": "node:22"},
        {"dns": _DNS, "packages": {"pip": ["x"]}},
        {"dns": _DNS, "packages": {"apt": "ripgrep"}},
        {"dns": {"nameservers": []}},
        {"dns": {"nameservers": ["one.one.one.one"]}},
        {"dns": {"nameservers": "system"}},
        {"dns": {"nameservers": ["host"]}},
        {"dns": {"nameservers": ["2606:4700:4700::1111"]}},
        {"dns": {"nameservers": ["1.1.1.1"], "search": ["corp"]}},
    ],
)
def test_anything_the_declaration_does_not_define_is_rejected(raw: object) -> None:
    """The base image is the recipe's, not the user's; a manager GuildBotics
    does not install with, an IPv6 or a hostname upstream have no meaning."""
    with pytest.raises(ToolchainError, match="^test: "):
        parse_toolchain(raw, where="test")


@pytest.mark.parametrize("spec", ["", "--force", "-y", "ripgrep 14.1", "a\tb"])
def test_a_package_entry_is_one_argument_and_never_an_option(spec: str) -> None:
    with pytest.raises(ToolchainError, match="package specification"):
        parse_toolchain({"dns": _DNS, "packages": {"apt": [spec]}}, where="t")


def test_the_template_is_a_valid_declaration() -> None:
    template = get_template_path() / TOOLCHAIN_PATH

    declaration = parse_toolchain(load_yaml_file(template), where="template")

    assert declaration == ToolchainDeclaration.model_validate(
        {"dns": {"nameservers": "host"}}
    )


def test_the_workspace_copy_wins_over_the_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".guildbotics" / "config"
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    assert load_toolchain().packages.npm == []

    target = config / TOOLCHAIN_PATH
    target.parent.mkdir(parents=True)
    target.write_text(
        "packages:\n  npm: ['typescript@5.6.3']\ndns:\n  nameservers: [10.0.0.53]\n"
    )

    assert load_toolchain().packages.npm == ["typescript@5.6.3"]
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

    with pytest.raises(ToolchainError, match="no IPv4 resolver; name the nameservers"):
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
