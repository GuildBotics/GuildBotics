"""Tests for the agent environment image's cargo wrapper.

The wrapper runs against a fake real cargo (REAL_CARGO) that records every call,
answers ``metadata`` the way Cargo does (the workspace of ``--manifest-path``
or else the nearest Cargo.toml above the working directory, and
CARGO_TARGET_DIR when set) and removes the target directory on ``clean``, and a
fake ``rustc`` on PATH whose ``-vV`` output the test sets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

WRAPPER = Path(__file__).parents[2] / "docker/agent-environment/cargo-wrapper.sh"
MANIFEST = """[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1"

[profile.dev]
debug = "line-tables-only"

[profile.dev.package."*"]
debug = false
"""
RUSTC = "rustc 1.96.0 (aaaaaaaaa 2026-01-01)\nhost: aarch64-unknown-linux-gnu\n"

FAKE_CARGO = """\
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]


def option(name):
    values = [b for a, b in zip(args, args[1:]) if a == name]
    values += [a.split("=", 1)[1] for a in args if a.startswith(name + "=")]
    return values[0] if values else None


def workspace():
    manifest = option("--manifest-path")
    if manifest:
        root = Path(manifest).resolve().parent
    else:
        root = next(
            (d for d in [Path.cwd(), *Path.cwd().parents] if (d / "Cargo.toml").is_file()),
            None,
        )
    if root is None or not (root / "Cargo.toml").is_file():
        sys.exit("error: could not find `Cargo.toml`")
    target = option("--target-dir") or os.environ.get("CARGO_TARGET_DIR") or root / "target"
    return root, Path(target).resolve()


entry = {"args": args, "target_dir": os.environ.get("CARGO_TARGET_DIR")}
if args[:1] == ["clean"]:
    entry["removed"] = str(workspace()[1])
with open(os.environ["CARGO_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")
if args[:1] == ["metadata"]:
    root, target = workspace()
    print(json.dumps({"target_directory": str(target), "workspace_root": str(root)}))
elif args[:1] == ["clean"]:
    shutil.rmtree(entry["removed"])
else:
    print("real cargo ran")
    sys.exit(int(os.environ.get("CARGO_EXIT", "0")))
"""

pytestmark = pytest.mark.usefixtures("shell_commands")


class Cargo:
    """Run the wrapper as a project's ``cargo`` and read what the real one did."""

    def __init__(self, tmp_path: Path) -> None:
        self.real = tmp_path / "rustup proxies/cargo"
        self.tools = tmp_path / "tools"
        self.log = tmp_path / "cargo.log"
        self.project = tmp_path / "project"
        self.target = self.project / "target"
        self.rustc = RUSTC
        self.env: dict[str, str] = {}
        for path, source in [
            (self.real, FAKE_CARGO),
            (
                self.tools / "rustc",
                "import os\nprint(os.environ['RUSTC_VV'], end='')\n",
            ),
        ]:
            path.parent.mkdir(parents=True)
            path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
            path.chmod(0o755)
        self.project.mkdir()
        (self.project / "Cargo.toml").write_text(MANIFEST, encoding="utf-8")

    def __call__(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.log.write_text("", encoding="utf-8")
        return subprocess.run(
            ["bash", str(WRAPPER), *args],
            cwd=cwd or self.project,
            env={
                # The developer's own Cargo settings (CARGO_TARGET_DIR above all)
                # would point the fake clean at a real directory.
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(("CARGO", "RUSTUP"))
                },
                "REAL_CARGO": str(self.real),
                "CARGO_LOG": str(self.log),
                "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
                "RUSTC_VV": self.rustc,
                **self.env,
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )

    def calls(self) -> list[list[str]]:
        """Return the arguments of every call the last run made to the real cargo."""
        return [entry["args"] for entry in self.entries()]

    def entries(self) -> list[dict[str, Any]]:
        """Return what the real cargo recorded of every call in the last run."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def cleans(self) -> list[str]:
        """Return the target directory each ``cargo clean`` of the last run removed."""
        return [entry["removed"] for entry in self.entries() if "removed" in entry]

    def build_stale(self, target: Path | None = None) -> Path:
        """Leave an output in the target directory and return it."""
        output = (target or self.target) / "debug/libold.rlib"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("old", encoding="utf-8")
        return output


@pytest.fixture
def cargo(tmp_path: Path) -> Cargo:
    return Cargo(tmp_path)


def assert_tagged_as_cache(target: Path) -> None:
    """The wrapper creates the directory, so it tags it as Cargo would."""
    tag = (target / "CACHEDIR.TAG").read_text(encoding="utf-8")
    assert tag.startswith("Signature: 8a477f597d28d172789f06886806bc55\n")


def test_target_without_a_mark_is_cleaned_and_marked(cargo: Cargo) -> None:
    stale = cargo.build_stale()

    result = cargo("build", "--release")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "real cargo ran\n"
    assert cargo.cleans() == [str(cargo.target)]
    assert cargo.calls()[-1] == ["build", "--release"]
    assert not stale.exists()
    mark = (cargo.target / ".build-conditions").read_text(encoding="utf-8")
    assert mark.startswith(RUSTC)
    assert '[profile.dev.package."*"]\ndebug = false\n' in mark
    assert "[dependencies]" not in mark
    assert_tagged_as_cache(cargo.target)


def test_missing_target_is_marked_without_cleaning(cargo: Cargo) -> None:
    cargo("build")

    assert cargo.cleans() == []
    assert (cargo.target / ".build-conditions").is_file()
    assert_tagged_as_cache(cargo.target)


@pytest.mark.parametrize(
    ("change", "cleans"),
    [
        ("nothing", False),
        ("rustc", True),
        ("profile", True),
        ("new profile", True),
        ("dependencies", False),
        ("package", False),
    ],
)
def test_build_conditions_decide_whether_to_clean(
    cargo: Cargo, change: str, cleans: bool
) -> None:
    cargo("build")
    stale = cargo.build_stale()
    manifest = cargo.project / "Cargo.toml"
    edits = {
        "profile": ('debug = "line-tables-only"', "debug = true"),
        "new profile": (
            "[dependencies]",
            "[profile.release]\nlto = true\n\n[dependencies]",
        ),
        "dependencies": ('serde = "1"', 'serde = "1"\ntokio = "1"'),
        "package": ('version = "0.1.0"', 'version = "0.2.0"'),
    }
    if change == "rustc":
        cargo.rustc = RUSTC.replace("1.96.0", "1.97.0")
    elif change in edits:
        old, new = edits[change]
        manifest.write_text(manifest.read_text().replace(old, new), encoding="utf-8")

    cargo("build")

    assert bool(cargo.cleans()) == cleans
    assert stale.exists() != cleans
    cargo("build")
    assert cargo.cleans() == [], "A second run under the same conditions must not clean"


def test_target_dir_from_the_environment_is_the_one_kept(
    cargo: Cargo, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "shared target"
    cargo.env["CARGO_TARGET_DIR"] = str(elsewhere)
    stale = cargo.build_stale(elsewhere)
    untouched = cargo.build_stale()

    cargo("test")

    assert cargo.cleans() == [str(elsewhere)]
    assert not stale.exists()
    assert untouched.exists()
    assert (elsewhere / ".build-conditions").is_file()
    assert not (cargo.target / ".build-conditions").exists()


@pytest.mark.parametrize("exit_code", [0, 101])
def test_outside_a_project_only_the_real_cargo_runs(
    cargo: Cargo, tmp_path: Path, exit_code: int
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    cargo.env["CARGO_EXIT"] = str(exit_code)
    args = ["install", "--root", "dir with spaces", "it's$HOME"]

    result = cargo(*args, cwd=outside)

    assert result.returncode == exit_code
    assert result.stdout == "real cargo ran\n"
    assert cargo.calls() == [["metadata", "--no-deps", "--format-version", "1"], args]
    assert list(outside.iterdir()) == []


def test_cargo_home_elsewhere_still_runs_the_real_cargo(
    cargo: Cargo, tmp_path: Path
) -> None:
    # A command may point CARGO_HOME at a registry it can write to.
    cargo.env["CARGO_HOME"] = str(tmp_path / "writable registry")
    stale = cargo.build_stale()

    result = cargo("test", "--locked")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "real cargo ran\n"
    assert cargo.calls()[-1] == ["test", "--locked"]
    assert not stale.exists()


@pytest.mark.parametrize(
    "spelling", [["--target-dir", "custom"], ["--target-dir=custom"]]
)
def test_target_dir_of_the_command_is_the_one_kept(
    cargo: Cargo, spelling: list[str]
) -> None:
    custom = cargo.project / "custom"
    stale = cargo.build_stale(custom)
    unused = cargo.build_stale()

    cargo("check", *spelling)

    assert cargo.cleans() == [str(custom)]
    assert not stale.exists()
    assert_tagged_as_cache(custom)
    assert (custom / ".build-conditions").is_file()
    assert unused.exists()
    assert not (cargo.target / ".build-conditions").exists()
    # The variable stands in for the option only in the wrapper's own calls.
    assert [entry["target_dir"] for entry in cargo.entries()] == [
        "custom",
        "custom",
        None,
    ]


@pytest.mark.parametrize(
    "spelling",
    [["--manifest-path", "project/Cargo.toml"], ["--manifest-path=project/Cargo.toml"]],
)
def test_manifest_path_of_the_command_is_followed_from_outside(
    cargo: Cargo, tmp_path: Path, spelling: list[str]
) -> None:
    stale = cargo.build_stale()

    result = cargo("check", *spelling, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert cargo.cleans() == [str(cargo.target)]
    assert not stale.exists()
    assert (cargo.target / ".build-conditions").is_file()


def test_arguments_after_the_separator_are_not_cargo_options(cargo: Cargo) -> None:
    custom = cargo.project / "custom"
    untouched = cargo.build_stale(custom)
    stale = cargo.build_stale()

    cargo("run", "--", "--target-dir", "custom")

    assert cargo.cleans() == [str(cargo.target)]
    assert not stale.exists()
    assert untouched.exists()
    assert cargo.calls()[-1] == ["run", "--", "--target-dir", "custom"]


@pytest.mark.parametrize(
    ("before", "after", "forwarded"),
    [
        (
            ["--config", 'build.build-dir="bd"'],
            [],
            ["--config", 'build.build-dir="bd"'],
        ),
        ([], ['--config=build.build-dir="bd"'], ['--config=build.build-dir="bd"']),
        (
            [],
            ["--manifest-path", "Cargo.toml", "--release", "--config", "a.toml"],
            ["--manifest-path", "Cargo.toml", "--config", "a.toml"],
        ),
        ([], ["--manifest-path=Cargo.toml"], ["--manifest-path=Cargo.toml"]),
    ],
)
def test_options_choosing_the_directories_reach_metadata_and_clean(
    cargo: Cargo, before: list[str], after: list[str], forwarded: list[str]
) -> None:
    # A build directory (build.build-dir) is cleaned only by a clean that is
    # given the same configuration.
    cargo.build_stale()

    cargo(*before, "check", *after)

    metadata, clean = [
        args for args in cargo.calls() if args[:1] in (["metadata"], ["clean"])
    ]
    assert metadata == ["metadata", "--no-deps", "--format-version", "1", *forwarded]
    assert clean == ["clean", *forwarded]
