"""Unpacking an artifact inside a command's environment: the module run there
with the archive on standard input."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from guildbotics.guest import artifact_archive
from guildbotics.utils.i18n_tool import t


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return output.getvalue()


def _unpack(archive: bytes, destination: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "guildbotics.guest.artifact_archive"]
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
    assert b"../outside.txt" in done.stderr
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
    run = [sys.executable, "-m", "guildbotics.guest.artifact_archive"]

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
    assert t("cli.member.attachment_exists", path=str(written)).encode() in again.stderr


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
    assert written.read_bytes() == b"png"


def test_an_attachment_with_no_plain_file_name_at_all_is_refused(tmp_path) -> None:
    with pytest.raises(
        artifact_archive.ArtifactError,
        match=re.escape(t("cli.member.attachment_name")),
    ):
        artifact_archive.place_attachment(
            io.BytesIO(b"png"), tmp_path, "../a.png", "../b"
        )


def test_case_alias_collision_is_translated_and_preserves_the_first_file(
    monkeypatch, tmp_path
) -> None:
    original = Path.open
    original_exists = Path.exists
    destination = tmp_path / "artifact"
    destination.mkdir()
    (destination / "A.txt").write_bytes(b"first")

    def absent_during_preflight(path):
        return False if path.name == "a.txt" else original_exists(path)

    def insensitive_open(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.name == "a.txt":
            path = path.with_name("A.txt")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", insensitive_open)
    monkeypatch.setattr(Path, "exists", absent_during_preflight)
    target = destination / "a.txt"
    with pytest.raises(
        artifact_archive.ArtifactError,
        match=re.escape(t("cli.member.artifact_exists", path=str(target))),
    ):
        artifact_archive.extract_artifact(
            io.BytesIO(_archive({"a.txt": b"second"})), destination
        )
    assert (destination / "A.txt").read_bytes() == b"first"
    assert [entry.name for entry in destination.iterdir()] == ["A.txt"]


@pytest.mark.parametrize(
    "files",
    [
        {"a": b"file", "a/child": b"child"},
        {"a/child": b"child", "a": b"file"},
        {"a": b"file", "a/": b""},
        {"a/": b"", "a": b"file"},
    ],
)
def test_archive_file_directory_conflicts_are_refused_before_writing(tmp_path, files):
    destination = tmp_path / "artifact"
    with pytest.raises(artifact_archive.ArtifactError):
        artifact_archive.extract_artifact(io.BytesIO(_archive(files)), destination)
    assert not destination.exists()


def test_existing_parent_file_is_refused_before_writing(tmp_path):
    destination = tmp_path / "artifact"
    destination.mkdir()
    (destination / "a").write_bytes(b"keep")
    with pytest.raises(
        artifact_archive.ArtifactError,
        match=re.escape(t("cli.member.artifact_exists", path=str(destination / "a"))),
    ):
        artifact_archive.extract_artifact(
            io.BytesIO(_archive({"good": b"good", "a/child": b"child"})), destination
        )
    assert [entry.name for entry in destination.iterdir()] == ["a"]
    assert (destination / "a").read_bytes() == b"keep"


@pytest.mark.parametrize("second", ["a/", "a/child"])
def test_case_alias_parent_collision_is_translated(monkeypatch, tmp_path, second):
    original = Path.mkdir
    original_exists = Path.exists
    destination = tmp_path / "artifact"
    destination.mkdir()
    (destination / "A").write_bytes(b"first")

    def absent_during_preflight(path):
        return False if path.name == "a" else original_exists(path)

    def insensitive_mkdir(path, *args, **kwargs):
        if path.name == "a" and path.with_name("A").is_file():
            raise FileExistsError(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", insensitive_mkdir)
    monkeypatch.setattr(Path, "exists", absent_during_preflight)
    with pytest.raises(artifact_archive.ArtifactError):
        artifact_archive.extract_artifact(
            io.BytesIO(_archive({second: b"second"})), destination
        )
    assert [entry.name for entry in destination.iterdir()] == ["A"]
    assert (destination / "A").read_bytes() == b"first"


@pytest.mark.parametrize("attachment", [False, True])
@pytest.mark.parametrize("existing_file", [False, True])
def test_relative_download_destinations_are_resolved_in_the_command_directory(
    tmp_path, attachment, existing_file
):
    directory = tmp_path / "command"
    directory.mkdir()
    destination = directory / "relative"
    if existing_file:
        destination.write_bytes(b"keep this file")
    arguments = [sys.executable, "-m", "guildbotics.guest.artifact_archive", "relative"]
    data = b"contents"
    if attachment:
        arguments.extend(["download.txt", "atc_1"])
    else:
        data = _archive({"download.txt": data})

    result = subprocess.run(
        arguments,
        cwd=directory,
        input=data,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if existing_file:
        assert result.returncode == 1
        assert str(destination.resolve()).encode() in result.stderr
        assert destination.read_bytes() == b"keep this file"
    else:
        assert result.returncode == 0, result.stderr
        assert (destination / "download.txt").read_bytes() == b"contents"
    assert not (tmp_path / "relative").exists()
