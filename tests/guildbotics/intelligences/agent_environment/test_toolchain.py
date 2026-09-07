"""The toolchain declaration: parsed strictly, read with the template fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.intelligences.agent_environment.toolchain import (
    TOOLCHAIN_PATH,
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
    parse_toolchain,
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
        {"dns": {"nameservers": ["1.1.1.1", "1.0.0.1"]}}
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
