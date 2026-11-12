"""Tests for the Cargo workspace's settings that CI depends on."""

from __future__ import annotations

import re
import shlex
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


def _crates() -> dict[str, Path]:
    """The workspace's crates by package name, with their directories."""
    return {
        _load(manifest.relative_to(ROOT).as_posix())["package"]["name"]: manifest.parent
        for manifest in (ROOT / "crates").glob("*/Cargo.toml")
    }


def _uses(name: str, tables: tuple[str, ...]) -> set[str]:
    """The workspace crates that a crate names in the given dependency tables,
    directly or through the workspace's `[workspace.dependencies]`."""
    crates = _crates()
    names = {directory.resolve(): crate for crate, directory in crates.items()}
    inherited = _load("Cargo.toml")["workspace"].get("dependencies", {})
    manifest = _load(f"{crates[name].relative_to(ROOT).as_posix()}/Cargo.toml")
    used = set()
    for scope in [manifest, *manifest.get("target", {}).values()]:
        for table in tables:
            for dependency, spec in scope.get(table, {}).items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("workspace") is True:
                    spec, base = inherited[dependency], ROOT
                else:
                    base = crates[name]
                if isinstance(spec, dict) and "path" in spec:
                    used.add(names[(base / spec["path"]).resolve()])
    return used


def _built_from(tested: set[str]) -> set[str]:
    """The tested crates and every workspace crate their tests build: the
    tested crates' dependencies of every kind, then what those need to build
    (their own dev-dependencies are no part of the tested crates' tests)."""
    built = set(tested)
    pending = set().union(
        *(
            _uses(name, ("dependencies", "dev-dependencies", "build-dependencies"))
            for name in tested
        )
    )
    while pending - built:
        name = (pending - built).pop()
        built.add(name)
        pending |= _uses(name, ("dependencies", "build-dependencies"))
    return built


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
        "gb-setup",
        "gb-environment",
        "gb-workspace",
        "gb-observability",
        "gb-wire",
        "loopback-auth",
        "gb-member",
        "gb-integrations",
        "gb-ports",
        "branvil",
    },
    "guildbotics": {
        "gb-base",
        "gb-server",
        "gb-environment",
        "branvil",
        "loopback-auth",
    },
}

# Provider integration tests exercise the member dispatcher; providers themselves
# must still depend only on the lower layers.
_ALLOWED_DEV = {"gb-integrations": {"gb-member"}}


def test_each_crate_uses_only_the_crates_below_it() -> None:
    assert set(_crates()) == set(_ALLOWED)
    for name, allowed in _ALLOWED.items():
        for table in ("dependencies", "dev-dependencies", "build-dependencies"):
            permitted = allowed | (
                _ALLOWED_DEV.get(name, set()) if table == "dev-dependencies" else set()
            )
            assert _uses(name, (table,)) <= permitted, (name, table)


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


def _tested_crates(job: dict) -> set[str]:
    """The packages that a CI job's `cargo nextest run` commands select."""
    tested: set[str] = set()
    for command in job["commands"]:
        for line in command.splitlines():
            words = shlex.split(line)
            starts = [
                i
                for i in range(len(words))
                if words[i : i + 3] == ["cargo", "nextest", "run"]
            ]
            if not starts:
                continue
            arguments = words[starts[0] + 3 :]
            selected = set(_crates()) if "--workspace" in arguments else set()
            for option, value in zip(arguments, arguments[1:]):
                if option in ("-p", "--package"):
                    selected.add(value)
                elif option == "--exclude":
                    selected.discard(value)
            tested |= selected
    return tested


def _rust_test_jobs() -> dict[str, dict]:
    jobs = {job["name"]: job for job in _load(".branvil/ci.toml")["jobs"]}
    return {name: job for name, job in jobs.items() if _tested_crates(job)}


def test_each_crate_is_tested_by_one_rust_test_job() -> None:
    jobs = {name: _tested_crates(job) for name, job in _rust_test_jobs().items()}

    assert set(jobs) == {"rust-test", "rust-test-branvil"}
    assert jobs["rust-test-branvil"] == {"branvil", "loopback-auth"}
    for crate in _crates():
        assert [name for name, tested in jobs.items() if crate in tested] in (
            ["rust-test"],
            ["rust-test-branvil"],
        ), crate


def test_rust_test_jobs_run_on_changes_to_what_their_tests_build_from() -> None:
    """A PR head skips a job whose paths match none of its changes, so each
    job's paths cover the workspace's Cargo settings and the files of every
    crate its tests build: otherwise a change reaches the job's tests only at
    the landing gate."""
    crates = _crates()
    workspace = [
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
        *ROOT.glob("rust-toolchain*"),
        *(path for path in (ROOT / ".cargo").rglob("*") if path.is_file()),
    ]
    for name, job in _rust_test_jobs().items():
        inputs = list(workspace)
        for crate in _built_from(_tested_crates(job)):
            directory = crates[crate]
            inputs += [directory / "Cargo.toml", *directory.glob("build.rs")]
            inputs += [
                path
                for part in ("src", "tests", "examples", "benches")
                for path in (directory / part).rglob("*")
                if path.is_file()
            ]
        uncovered = {
            path.relative_to(ROOT).as_posix()
            for path in inputs
            if not any(
                PurePosixPath(path.relative_to(ROOT).as_posix()).full_match(pattern)
                for pattern in job["paths"]
            )
        }
        assert uncovered == set(), name
