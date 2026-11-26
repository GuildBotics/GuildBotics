#!/usr/bin/env python3
"""Keep each verified Branvil build reachable by a single schema upgrade, and
mark a build that raises the schema as a release point."""

import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STORE = "crates/branvil/src/store.rs"
UPGRADE = "crates/branvil/src/upgrade.rs"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True)


def version(source: bytes) -> int:
    matches = re.findall(rb"^pub const SCHEMA_VERSION: u64 = ([0-9]+);$", source, re.M)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one SCHEMA_VERSION declaration in {STORE}")
    return int(matches[0])


def main() -> int:
    verified = os.environ.get("GB_CI_VERIFIED_SHA", "")
    if not verified:
        print("Skipping schema-raise: GB_CI_VERIFIED_SHA is empty or unset.")
        return 0
    sha = git("rev-parse", "--verify", "--end-of-options", f"{verified}^{{commit}}")
    verified = sha.stdout.decode().strip()
    before = version(git("show", f"{verified}:{STORE}").stdout)
    after = version((ROOT / STORE).read_bytes())
    reason = ""
    if after not in (before, before + 1):
        reason = "the checkout must keep the verified version or raise it by one"
    elif after == before and (ROOT / UPGRADE).read_bytes() != git(
        "show", f"{verified}:{UPGRADE}"
    ).stdout:
        reason = "upgrade.rs changed without raising the verified version"
    if reason:
        print(
            f"schema-raise failed: verified version {before}, checkout version {after}: "
            f"{reason}. Add changes to the upgrade for the still-unverified schema "
            "raise and combine them into a single step from the verified version.",
            file=sys.stderr,
        )
        return 1
    print(f"schema-raise passed: verified version {before}, checkout version {after}.")
    if after != before:
        # The Hub upgrades one step at a time: this commit is released by itself.
        print(f"::release-point::Branvil storage format {before} → {after}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"schema-raise could not inspect the schema: {error}", file=sys.stderr)
        sys.exit(1)
