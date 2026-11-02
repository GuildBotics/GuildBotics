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
