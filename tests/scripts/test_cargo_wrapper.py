"""Tests for the agent environment image's cargo wrapper.

The wrapper runs against a fake CARGO_HOME whose ``cargo`` records every call,
answers ``metadata`` the way Cargo does (the nearest Cargo.toml above the
working directory, CARGO_TARGET_DIR when set) and removes the target directory
on ``clean``, and a fake ``rustc`` on PATH whose ``-vV`` output the test sets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
with open(os.environ["CARGO_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"cwd": os.getcwd(), "args": args}) + "\\n")
if args[:1] == ["metadata"]:
    root = next(
        (d for d in [Path.cwd(), *Path.cwd().parents] if (d / "Cargo.toml").is_file()),
        None,
    )
    if root is None:
        sys.exit("error: could not find `Cargo.toml`")
    target = os.environ.get("CARGO_TARGET_DIR") or str(root / "target")
    print(json.dumps({"target_directory": target, "workspace_root": str(root)}))
elif args[:1] == ["clean"]:
    shutil.rmtree(args[args.index("--target-dir") + 1])
else:
    print("real cargo ran")
    sys.exit(int(os.environ.get("CARGO_EXIT", "0")))
"""

pytestmark = pytest.mark.usefixtures("shell_commands")


class Cargo:
    """Run the wrapper as a project's ``cargo`` and read what the real one did."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "cargo home"
        self.tools = tmp_path / "tools"
        self.log = tmp_path / "cargo.log"
        self.project = tmp_path / "project"
        self.target = self.project / "target"
        self.rustc = RUSTC
        self.env: dict[str, str] = {}
        for path, source in [
            (self.home / "bin/cargo", FAKE_CARGO),
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
                "CARGO_HOME": str(self.home),
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
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["args"] for line in lines]

    def cleans(self) -> list[list[str]]:
        """Return the ``cargo clean`` calls of the last run."""
        return [args for args in self.calls() if args[:1] == ["clean"]]

    def build_stale(self, target: Path | None = None) -> Path:
        """Leave an output in the target directory and return it."""
        output = (target or self.target) / "debug/libold.rlib"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("old", encoding="utf-8")
        return output


@pytest.fixture
def cargo(tmp_path: Path) -> Cargo:
    return Cargo(tmp_path)


def test_target_without_a_mark_is_cleaned_and_marked(cargo: Cargo) -> None:
    stale = cargo.build_stale()

    result = cargo("build", "--release")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "real cargo ran\n"
    assert cargo.cleans() == [
        [
            "clean",
            "--manifest-path",
            str(cargo.project / "Cargo.toml"),
            "--target-dir",
            str(cargo.target),
        ]
    ]
    assert cargo.calls()[-1] == ["build", "--release"]
    assert not stale.exists()
    mark = (cargo.target / ".build-conditions").read_text(encoding="utf-8")
    assert mark.startswith(RUSTC)
    assert '[profile.dev.package."*"]\ndebug = false\n' in mark
    assert "[dependencies]" not in mark


def test_missing_target_is_marked_without_cleaning(cargo: Cargo) -> None:
    cargo("build")

    assert cargo.cleans() == []
    assert (cargo.target / ".build-conditions").is_file()


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

    assert [args[-1] for args in cargo.cleans()] == [str(elsewhere)]
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
