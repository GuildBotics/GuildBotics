"""Tests for the Cargo workspace's settings that CI depends on."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).parents[1]


def _load(path: str) -> dict:
    return tomllib.loads((ROOT / path).read_text(encoding="utf-8"))


def test_the_toolchain_is_the_rust_of_the_ci_image() -> None:
    dockerfile = (ROOT / "docker/agent-environment/Dockerfile").read_text("utf-8")
    [image_rust] = re.findall(r"^ARG RUST_VERSION=(\S+)$", dockerfile, re.MULTILINE)

    assert _load("rust-toolchain.toml")["toolchain"]["channel"] == image_rust


def test_the_ci_caches_under_the_checkout_are_no_part_of_the_workspace() -> None:
    """uv builds Python sdists with Cargo inside its cache: a cache directory
    under the workspace's root would make their manifests look like members."""
    workspace = _load("Cargo.toml")["workspace"]
    cached = {
        PurePosixPath(path).parts[0]
        for job in _load(".branvil/ci.toml")["jobs"]
        for path in job.get("cache", {}).get("paths", [])
    }

    assert cached - {"target"} <= set(workspace["exclude"])


#: Every crate of the workspace, by the workspace crates it may use. Branvil
#: and what Branvil and GuildBotics share know nothing of GuildBotics.
#: GuildBotics' crates follow the layers of the Python version's zones (#81):
#: each uses only crates below it, and siblings of one layer (workspace and
#: observability) never each other. The wire, generated from the schemas,
#: and the service ports both stand on no other workspace crate; integrations
#: belong above the ports. Member capabilities use ports and the workspace's
#: identity and record stores, without depending on a provider or the server.
_ALLOWED = {
    "loopback-auth": set(),
    "branvil": {"loopback-auth"},
    "gb-base": set(),
    "gb-workspace": {"gb-base"},
    "gb-observability": {"gb-base"},
    "gb-wire": set(),
    "gb-ports": set(),
    "gb-integrations": {"gb-ports", "gb-base", "gb-config", "branvil"},
    "gb-member": {"gb-ports", "gb-base", "gb-workspace", "gb-observability"},
    "gb-config": {"gb-base", "gb-wire"},
    "gb-setup": {"gb-base", "gb-config"},
    "gb-environment": {"gb-base", "gb-config", "gb-wire"},
    "gb-server": {
        "gb-base",
        "gb-config",
        "gb-environment",
        "gb-workspace",
        "gb-observability",
        "gb-wire",
        "loopback-auth",
    },
    "guildbotics": {
        "gb-base",
        "gb-server",
        "gb-environment",
        "branvil",
        "loopback-auth",
    },
}


def test_each_crate_uses_only_the_crates_below_it() -> None:
    crates = {path.parent.name for path in (ROOT / "crates").glob("*/Cargo.toml")}
    assert crates == set(_ALLOWED)
    for name, allowed in _ALLOWED.items():
        manifest = _load(f"crates/{name}/Cargo.toml")
        scopes = [manifest, *manifest.get("target", {}).values()]
        used = {
            dependency
            for scope in scopes
            for table in ("dependencies", "dev-dependencies", "build-dependencies")
            for dependency, spec in scope.get(table, {}).items()
            if isinstance(spec, dict) and "path" in spec
        }

        assert used <= allowed, name


def test_only_the_environment_drives_the_microvm_runtime() -> None:
    """One crate speaks to the runtime, as one module does in the Python
    version (the ``one-sandbox-runtime`` contract)."""
    users = set()
    for path in (ROOT / "crates").glob("*/Cargo.toml"):
        manifest = _load(path.relative_to(ROOT).as_posix())
        for scope in [manifest, *manifest.get("target", {}).values()]:
            for table in ("dependencies", "dev-dependencies", "build-dependencies"):
                if "microsandbox" in scope.get(table, {}):
                    users.add(path.parent.name)

    assert users == {"gb-environment"}


def test_the_runtime_crates_and_version_check_are_pinned() -> None:
    crates = _load("crates/gb-environment/Cargo.toml")["dependencies"]
    runtime = (ROOT / "crates/gb-environment/src/runtime.rs").read_text("utf-8")
    [checked] = re.findall(r'RUNTIME_VERSION: &str = "([^"]+)"', runtime)
    assert crates["microsandbox"]["version"] == f"={checked}"
    assert crates["microsandbox-utils"] == f"={checked}"
