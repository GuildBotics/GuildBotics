"""Write what the host downloaded for a member command where it is to be
read: a CI artifact unpacked, or a ticket's attachment as one file.

A member command a command of an isolated environment asks for writes it
inside that environment: the host downloads it, and this module, run there
(``python -m``, with the download on standard input, the destination as its
argument, and an attachment's name and id after it), writes it. The host
writes nothing the environment can write, which it could swap for a link to
anywhere while the download runs.
"""

from __future__ import annotations

import json
import re
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
#: The ticket services keep images of at most 10 MiB; an attachment is
#: written to the isolated workspace as an artifact is.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
#: What a file name written here may not hold, on any host.
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class ArtifactError(ValueError):
    """A download that cannot be written safely; the message says why."""


class Unpacked(TypedDict):
    """Where a download was written, as its reader reports it."""

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
        raise ArtifactError("CI artifact is not a valid ZIP archive.") from exc


def place_attachment(
    content: IO[bytes], destination: Path, name: str, attachment_id: str
) -> Path:
    """Write ``content`` into ``destination`` as the file ``name``, or, for a
    name that is not one plain file name, as ``attachment_id``.

    Returns:
        The file written.

    Raises:
        ArtifactError: If neither is a plain file name, or the file is
            already there.
    """
    file_name = next(
        (
            each
            for each in (name, attachment_id)
            if each.strip(" .") and not _UNSAFE_NAME.search(each)
        ),
        None,
    )
    if file_name is None:
        raise ArtifactError(f"Attachment has no safe file name: {attachment_id!r}")
    destination = destination.resolve()
    target = destination / file_name
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as output:
            copyfileobj(content, output)
    except FileExistsError:
        raise ArtifactError(
            f"Attachment destination already exists: {target}. "
            "Choose a different --dest or remove the existing file, then retry."
        ) from None
    return target


def write_download(
    download: IO[bytes], destination: Path, *attachment: str
) -> list[Path]:
    """Write ``download`` into ``destination``: unpacked as an artifact, or,
    given an attachment's name and id, as that attachment.

    Returns:
        The files written.

    Raises:
        ArtifactError: If it cannot be written safely.
    """
    if attachment:
        return [place_attachment(download, destination, *attachment)]
    return extract_artifact(download, destination)


def unpacked(destination: Path, files: list[Path]) -> Unpacked:
    """Where the download was written, as its reader reports it."""
    return {
        "destination": str(destination.resolve()),
        "files": [str(path.resolve()) for path in files],
    }


def main(arguments: list[str]) -> int:
    """Write the download on standard input into ``arguments[0]``: unpacked
    as an artifact, or, with an attachment's name and id after it, as that
    attachment; and write where it went as JSON. What cannot be written
    safely is refused on standard error.
    """
    destination, attachment = Path(arguments[0]), arguments[1:]
    with tempfile.TemporaryFile() as download:
        copied = 0
        while chunk := sys.stdin.buffer.read(1 << 16):
            copied += len(chunk)
            if copied > MAX_ARTIFACT_BYTES:
                print(
                    f"The download is above the {MAX_ARTIFACT_BYTES} byte limit.",
                    file=sys.stderr,
                )
                return 1
            download.write(chunk)
        download.seek(0)
        try:
            files = write_download(download, destination, *attachment)
        except ArtifactError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(json.dumps(unpacked(destination, files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
