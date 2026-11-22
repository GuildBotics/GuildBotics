"""Exercise the schema guard against real, disposable Git histories."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/check-schema-raise.py"
STORE = "crates/branvil/src/store.rs"
UPGRADE = "crates/branvil/src/upgrade.rs"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


def store(repo: Path, number: int) -> None:
    (repo / STORE).write_text(f"pub const SCHEMA_VERSION: u64 = {number};\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Example")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "example@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Example")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "example@example.invalid")
    git(tmp_path, "init", "-q")
    (tmp_path / STORE).parent.mkdir(parents=True)
    (tmp_path / SCRIPT).parent.mkdir()
    shutil.copyfile(ROOT / SCRIPT, tmp_path / SCRIPT)
    store(tmp_path, 25)
    (tmp_path / UPGRADE).write_bytes(b"original upgrade\n")
    commit(tmp_path)
    return tmp_path


def run(repo: Path, verified: str | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("GB_CI_VERIFIED_SHA", None)
    if verified is not None:
        env["GB_CI_VERIFIED_SHA"] = verified
    return subprocess.run(
        [sys.executable, str(repo / SCRIPT)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("number", "changed", "success"),
    [
        (26, True, True),
        (25, False, True),
        (27, True, False),
        (25, True, False),
        (24, False, False),
    ],
)
def test_schema_steps(repo: Path, number: int, changed: bool, success: bool) -> None:
    verified = git(repo, "rev-parse", "HEAD")
    store(repo, number)
    if changed:
        (repo / UPGRADE).write_bytes(b"changed upgrade\n")
    result = run(repo, verified)
    assert (result.returncode == 0) is success, result.stderr
    output = result.stdout + result.stderr
    assert "25" in output and str(number) in output
    if not success:
        assert "still-unverified" in output and "single step" in output


@pytest.mark.parametrize("verified", [None, ""])
def test_no_verified_commit(repo: Path, verified: str | None) -> None:
    result = run(repo, verified)
    assert result.returncode == 0
    assert "empty or unset" in result.stdout


def test_invalid_commit_fails(repo: Path) -> None:
    result = run(repo, "0" * 40)
    assert result.returncode == 1
    assert "could not inspect" in result.stderr


@pytest.mark.parametrize("target", ["verified", "checkout"])
def test_missing_version_fails(repo: Path, target: str) -> None:
    verified = git(repo, "rev-parse", "HEAD")
    (repo / STORE).write_text("no declaration\n")
    if target == "verified":
        verified = commit(repo)
        store(repo, 26)
    result = run(repo, verified)
    assert result.returncode == 1
    assert "exactly one SCHEMA_VERSION" in result.stderr


def test_repository_itself() -> None:
    result = run(ROOT, "HEAD")
    assert result.returncode == 0, result.stderr


def test_ci_schema_command_runs_without_a_virtualenv(repo: Path) -> None:
    jobs = tomllib.loads((ROOT / ".guildbotics-ci.toml").read_text())["jobs"]
    [job] = [job for job in jobs if job["name"] == "schema-raise"]
    # Match the image's command names without a virtualenv's bare python.
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    for name in ("python3", "git"):
        executable = shutil.which(name)
        assert executable is not None
        (bin_dir / name).symlink_to(executable)
    env = {**os.environ, "PATH": str(bin_dir), "GB_CI_VERIFIED_SHA": "HEAD"}
    for command in job["commands"]:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (
            "schema-raise passed: verified version 25, checkout version 25"
            in result.stdout
        )


def test_ci_schema_job() -> None:
    jobs = {
        job["name"]: job
        for job in tomllib.loads((ROOT / ".guildbotics-ci.toml").read_text())["jobs"]
    }
    job = jobs["schema-raise"]
    assert job["verified_sha"] is True
    assert "cache" not in job
    assert job["commands"] == [f"python3 {SCRIPT}"]
    assert "verified_sha" not in jobs["test"]
