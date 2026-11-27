"""Check the Git tag selection used for Python package versions."""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_hub_release_tags_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    def commit(message: str) -> None:
        git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        )

    git("init", "-q")
    commit("Python release")
    git("tag", "v1.2.3")
    commit("Hub release")
    git("tag", "release/2026-11-27-1")

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = pyproject["tool"]["hatch"]["version"]["raw-options"]
    describe = options["scm"]["git"]["describe_command"]
    assert subprocess.check_output(describe, cwd=tmp_path, text=True).startswith(
        "v1.2.3-1-g"
    )
