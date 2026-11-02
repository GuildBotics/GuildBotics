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


#: The workspace's crates that know nothing of GuildBotics, by the workspace
#: crates each may use: Branvil, and what Branvil and GuildBotics share.
_NEUTRAL = {"loopback-auth": set(), "branvil": {"loopback-auth"}}


def test_branvil_and_the_shared_crates_know_nothing_of_guildbotics() -> None:
    for name, allowed in _NEUTRAL.items():
        manifest = _load(f"crates/{name}/Cargo.toml")
        used = {
            dependency
            for table in ("dependencies", "dev-dependencies", "build-dependencies")
            for dependency, spec in manifest.get(table, {}).items()
            if isinstance(spec, dict) and "path" in spec
        }

        assert used <= allowed, name
