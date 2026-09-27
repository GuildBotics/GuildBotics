"""Unpacking an artifact inside a command's environment: the module run there
with the archive on standard input."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from guildbotics.capabilities import artifact_archive


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return output.getvalue()


def _unpack(archive: bytes, destination: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "guildbotics.capabilities.artifact_archive"]
        + [str(destination)],
        input=archive,
        capture_output=True,
        check=False,
    )


def test_the_environment_unpacks_the_archive_it_is_given(tmp_path) -> None:
    destination = tmp_path / "artifact"

    done = _unpack(_archive({"report/error.md": b"details"}), destination)

    assert done.returncode == 0, done.stderr
    written = destination.resolve() / "report" / "error.md"
    assert json.loads(done.stdout) == {
        "destination": str(destination.resolve()),
        "files": [str(written)],
    }
    assert written.read_bytes() == b"details"


def test_an_unsafe_archive_is_refused_with_the_reason(tmp_path) -> None:
    done = _unpack(_archive({"../outside.txt": b"escape"}), tmp_path / "artifact")

    assert done.returncode == 1
    assert b"unsafe path: ../outside.txt" in done.stderr
    assert not (tmp_path / "outside.txt").exists()


def test_an_archive_above_the_limit_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(artifact_archive, "MAX_ARTIFACT_BYTES", 16)
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(_archive({"a": b"x" * 64})))
    )

    assert artifact_archive.main([str(tmp_path / "artifact")]) == 1
    assert not (tmp_path / "artifact").exists()


@pytest.mark.parametrize("name", ["", "."])
def test_an_archive_naming_its_destination_itself_is_refused(tmp_path, name) -> None:
    done = _unpack(_archive({name or "./": b""}), tmp_path / "artifact")

    assert done.returncode == 1
