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


def test_the_environment_writes_an_attachment_as_one_file(tmp_path) -> None:
    destination = tmp_path / "out"
    run = [sys.executable, "-m", "guildbotics.capabilities.artifact_archive"]

    done = subprocess.run(
        [*run, str(destination), "screen.png", "atc_1"],
        input=b"png",
        capture_output=True,
        check=False,
    )
    again = subprocess.run(
        [*run, str(destination), "screen.png", "atc_1"],
        input=b"other",
        capture_output=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    written = destination.resolve() / "screen.png"
    assert json.loads(done.stdout) == {
        "destination": str(destination.resolve()),
        "files": [str(written)],
    }
    assert (again.returncode, written.read_bytes()) == (1, b"png")
    assert b"already exists" in again.stderr


@pytest.mark.parametrize(
    "name", ["../up.png", "a/b.png", "a\b.png", "c:d.png", "..", " ", "a\nb"]
)
def test_an_attachment_not_named_by_one_plain_file_name_is_written_by_its_id(
    tmp_path, name
) -> None:
    written = artifact_archive.place_attachment(
        io.BytesIO(b"png"), tmp_path / "out", name, "atc_1"
    )

    assert written == (tmp_path / "out" / "atc_1").resolve()
    assert [path.name for path in tmp_path.rglob("*")] == ["out", "atc_1"]


def test_an_attachment_with_no_plain_file_name_at_all_is_refused(tmp_path) -> None:
    with pytest.raises(artifact_archive.ArtifactError, match="no safe file name"):
        artifact_archive.place_attachment(
            io.BytesIO(b"png"), tmp_path, "../a.png", "../b"
        )
