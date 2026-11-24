"""Exercise CI cache maintenance with Cargo and a file-only registry."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / ".cargo/ci-cache.py"
DEP = "local-dependency"
FEATURES = ("one", "two", "three")
DEPENDENCY = """[package]
name = "local-dependency"
version = "{version}"
edition = "2021"

[features]
one = []
two = []
three = []
"""
APP = """[package]
name = "cache-test"
version = "0.1.0"
edition = "2021"

[dependencies]
local-dependency = {{ version = "={version}", features = ["{feature}"] }}

[profile.dev]
debug = false
"""


class CargoCache:
    def __init__(self, root: Path, cargo: str) -> None:
        self.root = root
        self.cargo = cargo
        self.target = root / "target"
        self.home = root / ".ci" / "cargo-home"
        self.env = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CARGO_", "RUST")) or key == "RUSTUP_HOME"
            },
            "CARGO_HOME": str(self.home),
            "CARGO_INCREMENTAL": "0",
            "CARGO_NET_OFFLINE": "true",
        }
        shutil.copyfile(ROOT / "rust-toolchain.toml", root / "rust-toolchain.toml")
        registry = root / "registry"
        index = registry / "index" / "lo" / "ca" / DEP
        index.parent.mkdir(parents=True)
        index.write_text(
            "".join(
                json.dumps(make_crate(registry, version)) + "\n"
                for version in ("1.0.0", "2.0.0")
            ),
            encoding="utf-8",
        )
        (root / ".cargo").mkdir()
        (root / ".cargo" / "config.toml").write_text(
            """[source.crates-io]
replace-with = "local"

[source.local]
local-registry = "registry"
""",
            encoding="utf-8",
        )
        (root / "src").mkdir()
        (root / "src" / "lib.rs").write_text(
            "pub use local_dependency::value;\n"
            "#[test] fn dependency_links() { assert_eq!(value()[0], 7); }\n",
            encoding="utf-8",
        )

    def manifest(self, feature: str, version: str = "2.0.0") -> None:
        (self.root / "Cargo.toml").write_text(
            APP.format(feature=feature, version=version), encoding="utf-8"
        )

    def run(self, *command: str) -> str:
        result = subprocess.run(
            command,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def build(self, *args: str) -> None:
        self.run(sys.executable, str(SCRIPT), "run", self.cargo, "build", *args)

    def clean_workspace(self) -> None:
        self.run(self.cargo, "clean", "--workspace", "--offline", "--locked")

    def prune(self) -> None:
        self.run(sys.executable, str(SCRIPT), "prune")

    def dependency_is_fresh(self, *args: str) -> bool:
        output = self.run(self.cargo, "build", "--message-format=json", *args)
        artifacts = [
            message["fresh"]
            for message in map(json.loads, output.splitlines())
            if message["reason"] == "compiler-artifact"
            and message["target"]["name"] in ("local_dependency", "build-script-build")
        ]
        return len(artifacts) == 2 and all(artifacts)

    def cached_dependency_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for folder in ("deps", "build", ".fingerprint")
            for path in (self.target / "debug" / folder).rglob("*")
            if path.is_file()
        )


def make_crate(registry: Path, version: str) -> dict:
    archive = registry / f"{DEP}-{version}.crate"
    with tarfile.open(archive, "w:gz") as tar:
        for name, source in {
            "Cargo.toml": DEPENDENCY.format(version=version),
            "src/lib.rs": "pub static VALUE: [u8; 8192] = [7; 8192];\n"
            "pub fn value() -> &'static [u8] { &VALUE }\n",
            "build.rs": 'fn main() { println!("cargo:rerun-if-changed=build.rs"); }\n',
        }.items():
            data = source.encode()
            header = tarfile.TarInfo(f"{DEP}-{version}/{name}")
            header.size = len(data)
            tar.addfile(header, io.BytesIO(data))
    return {
        "name": DEP,
        "vers": version,
        "deps": [],
        "cksum": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "features": {feature: [] for feature in FEATURES},
        "yanked": False,
    }


@pytest.fixture
def cache(tmp_path: Path) -> CargoCache:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("Cargo is unavailable")
    return CargoCache(tmp_path, cargo)


def test_three_generations_stay_within_five_percent_of_a_cold_build(
    cache: CargoCache,
) -> None:
    for feature in FEATURES:
        cache.manifest(feature)
        cache.build()
        cache.clean_workspace()
        cache.prune()
    warm = cache.cached_dependency_bytes()
    assert cache.dependency_is_fresh("--locked")

    shutil.rmtree(cache.target)
    cache.run(cache.cargo, "build", "--locked")
    cache.clean_workspace()
    cold = cache.cached_dependency_bytes()
    assert cold > 0
    assert abs(warm - cold) <= cold * 0.05, (warm, cold)


def test_prune_keeps_artifacts_from_each_build(cache: CargoCache) -> None:
    cache.manifest("one")
    cache.build()
    cache.manifest("two")
    cache.build("--locked")
    cache.clean_workspace()
    cache.prune()

    assert cache.dependency_is_fresh("--locked")
    cache.manifest("one")
    assert cache.dependency_is_fresh("--locked")


def test_prune_keeps_nextest_artifacts_for_an_explicit_target(
    cache: CargoCache,
) -> None:
    if shutil.which("cargo-nextest") is None:
        pytest.skip("Cargo nextest is unavailable")
    version = cache.run(cache.cargo, "-vV")
    host = next(
        line.removeprefix("host: ")
        for line in version.splitlines()
        if line.startswith("host: ")
    )
    cache.manifest("one")
    cache.run(
        sys.executable,
        str(SCRIPT),
        "run",
        cache.cargo,
        "nextest",
        "run",
        "--target",
        host,
    )
    cache.clean_workspace()
    cache.prune()

    assert cache.dependency_is_fresh("--locked", "--target", host)


def test_prune_removes_registry_crates_missing_from_lockfile(cache: CargoCache) -> None:
    cache.manifest("one", "1.0.0")
    cache.run(cache.cargo, "build")
    cache.manifest("one")
    cache.build()

    # A local registry supplies the archives directly, so Cargo only caches
    # its extracted sources. Populate the archive cache with the same crates.
    registry = cache.home / "registry"
    source = next((registry / "src").iterdir())
    archives = registry / "cache" / source.name
    archives.mkdir(parents=True)
    for name in (f"{DEP}-1.0.0", f"{DEP}-2.0.0"):
        assert (source / name / "Cargo.toml").is_file()
        shutil.copyfile(
            cache.root / "registry" / f"{name}.crate", archives / f"{name}.crate"
        )
    cache.clean_workspace()
    cache.prune()

    assert {path.name for path in source.iterdir()} == {f"{DEP}-2.0.0"}
    assert {path.name for path in archives.iterdir()} == {f"{DEP}-2.0.0.crate"}
    assert cache.dependency_is_fresh("--locked")
