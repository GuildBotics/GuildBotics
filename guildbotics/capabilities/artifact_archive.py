"""Unpack a GitHub Actions artifact where it is to be read.

A member command a command of an isolated environment asks for unpacks its
artifact inside that environment: the host downloads it, and this module,
run there (``python -m``, with the archive on standard input and the
destination as its argument), unpacks it. The host writes nothing the
environment can write, which it could swap for a link to anywhere while the
artifact downloads.
"""

from __future__ import annotations

import json
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from typing import IO, Any, TypedDict
from zipfile import BadZipFile, ZipFile

# Artifacts are written to the isolated workspace instead of the broker output,
# so they can be larger than the shared stdout boundary while remaining bounded.
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


class ArtifactError(ValueError):
    """An artifact that cannot be unpacked safely; the message says why."""


class Unpacked(TypedDict):
    """Where an artifact was unpacked, as its reader reports it."""

    destination: str
    files: list[str]


def extract_artifact(archive: IO[bytes], destination: Path) -> list[Path]:
    """Unpack ``archive`` into ``destination``, refusing anything unsafe.

    Returns:
        The files written.

    Raises:
        ArtifactError: For an archive too large when expanded, one naming a
            path outside ``destination``, a link, a path twice, or a file
            already there, and for one that is no ZIP archive.
    """
    destination = destination.resolve()
    try:
        with ZipFile(archive) as bundle:
            members = bundle.infolist()
            total_size = sum(member.file_size for member in members)
            if total_size > MAX_ARTIFACT_BYTES:
                raise ArtifactError(
                    "Expanded artifact is "
                    f"{total_size} bytes, above the {MAX_ARTIFACT_BYTES} byte limit."
                )
            targets: list[tuple[Any, Path]] = []
            seen: set[Path] = set()
            for member in members:
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                if (
                    relative == PurePosixPath(".")
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    raise ArtifactError(
                        f"Artifact contains an unsafe path: {member.filename}"
                    )
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ArtifactError(
                        f"Artifact contains an unsupported symlink: {member.filename}"
                    )
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(destination):
                    raise ArtifactError(
                        f"Artifact contains an unsafe path: {member.filename}"
                    )
                if target in seen:
                    raise ArtifactError(
                        f"Artifact contains a duplicate path: {member.filename}"
                    )
                seen.add(target)
                targets.append((member, target))
            collisions = [
                target
                for member, target in targets
                if not member.is_dir() and target.exists()
            ]
            if collisions:
                raise ArtifactError(
                    f"Artifact destination already exists: {collisions[0]}. "
                    "Choose a different --dest or remove the existing file, then retry."
                )
            destination.mkdir(parents=True, exist_ok=True)
            files: list[Path] = []
            for member, target in targets:
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    copyfileobj(source, output)
                files.append(target)
            return files
    except BadZipFile as exc:
        raise ArtifactError("GitHub artifact is not a valid ZIP archive.") from exc


def unpacked(destination: Path, files: list[Path]) -> Unpacked:
    """Where the artifact was unpacked, as its reader reports it."""
    return {
        "destination": str(destination.resolve()),
        "files": [str(path.resolve()) for path in files],
    }


def main(arguments: list[str]) -> int:
    """Unpack the archive on standard input into ``arguments[0]``, and write
    what was unpacked as JSON; an unsafe archive is refused on standard error.
    """
    destination = Path(arguments[0])
    with tempfile.TemporaryFile() as archive:
        copied = 0
        while chunk := sys.stdin.buffer.read(1 << 16):
            copied += len(chunk)
            if copied > MAX_ARTIFACT_BYTES:
                print(
                    f"Artifact is above the {MAX_ARTIFACT_BYTES} byte limit.",
                    file=sys.stderr,
                )
                return 1
            archive.write(chunk)
        archive.seek(0)
        try:
            files = extract_artifact(archive, destination)
        except ArtifactError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(json.dumps(unpacked(destination, files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
