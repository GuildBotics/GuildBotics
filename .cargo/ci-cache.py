"""Keep the Cargo units used by this CI job and the crates in its lockfile."""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

REPORT = Path(".ci/cargo-cache-artifacts.jsonl")
UNIT_HASH = re.compile(r"-([0-9a-f]{16})(?:\.|$)")


def run(command: list[str]) -> int:
    """Record a successful build's outputs while preserving its diagnostics."""
    option = (
        "--cargo-message-format=json"
        if command[1:3] == ["nextest", "run"]
        else "--message-format=json"
    )
    end = command.index("--") if "--" in command else len(command)
    command.insert(end, option)
    paths: set[str] = set()
    finished = False
    with subprocess.Popen(command, stdout=subprocess.PIPE, encoding="utf-8") as cargo:
        assert cargo.stdout is not None
        for line in cargo.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(line, end="", flush=True)
                continue
            match message.get("reason"):
                case "compiler-artifact":
                    paths.update(message["filenames"])
                case "build-script-executed":
                    paths.add(message["out_dir"])
                case "compiler-message":
                    diagnostic = message["message"]
                    print(
                        diagnostic.get("rendered") or diagnostic["message"],
                        end="",
                        file=sys.stderr,
                    )
                case "build-finished":
                    finished = message["success"]
                case _:
                    print(line, end="", flush=True)
        if status := cargo.wait():
            return status
    if not finished or not paths:
        raise SystemExit("Cargo did not report a complete build for cache pruning")
    REPORT.parent.mkdir(exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as report:
        report.write(json.dumps(sorted(paths)) + "\n")
    return 0


def unit_hash(name: str) -> str | None:
    match = UNIT_HASH.search(name)
    return match[1] if match else None


def used_units(target: Path) -> dict[Path, set[str]]:
    """Find each profile's unit hashes from its deps and build-script paths."""
    units: dict[Path, set[str]] = defaultdict(set)
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        for name in json.loads(line):
            path = Path(name).resolve()
            if not path.is_relative_to(target):
                continue
            if path.parent.name == "deps":
                unit_path = path
            elif path.parent.parent.name == "build":
                # The executable and OUT_DIR use separate hashes.
                unit_path = path.parent
            else:
                continue
            if unit := unit_hash(unit_path.name):
                units[unit_path.parent.parent].add(unit)
    if not units:
        raise SystemExit("Cargo did not report any units in the CI target directory")
    return units


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def prune() -> None:
    target = Path.cwd().resolve() / "target"
    registry = Path(os.environ["CARGO_HOME"]).resolve() / "registry"
    units = used_units(target)
    with Path("Cargo.lock").open("rb") as lock:
        packages = {
            f"{package['name']}-{package['version']}"
            for package in tomllib.load(lock)["package"]
            if package.get("source", "").startswith("registry+")
        }

    # Cargo 1.96 nests explicit targets one level deeper than the host
    # profile. Preserve companion files (such as .d) and fingerprints of
    # every unit Cargo reported, including units that were already fresh.
    for kind in ("deps", "build", ".fingerprint"):
        for folder in (*target.glob(f"*/{kind}"), *target.glob(f"*/*/{kind}")):
            if not folder.is_dir() or folder.resolve() != folder:
                continue
            keep = units.get(folder.parent, set())
            for path in folder.iterdir():
                if (unit := unit_hash(path.name)) and unit not in keep:
                    remove(path)

    # Registry indexes are resolution metadata. Only the downloaded archive
    # and its unpacked sources are package contents.
    for directory, pattern, suffix in (
        ("cache", "*.crate", ".crate"),
        ("src", "*", ""),
    ):
        for source in registry.glob(f"{directory}/*"):
            if not source.is_dir() or source.resolve() != source:
                continue
            for path in source.glob(pattern):
                if (
                    (suffix or path.is_dir())
                    and path.name.removesuffix(suffix) not in packages
                ):
                    remove(path)
    REPORT.unlink()


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"] and len(sys.argv) > 3:
        sys.exit(run(sys.argv[2:]))
    if sys.argv[1:] == ["prune"]:
        prune()
    else:
        sys.exit("usage: ci-cache.py run cargo <args> | prune")
