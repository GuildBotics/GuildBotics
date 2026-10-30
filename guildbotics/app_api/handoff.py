"""The exchange directory as a browser sees it: what commands produced for
the user, listed and handed over as downloads, since a browser on another
machine cannot open the server machine's files."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from guildbotics.app_api.errors import AppApiError
from guildbotics.environment.contract import exchange_dir
from guildbotics.utils.safe_paths import read_host_file

#: The newest files the listing shows.
MAX_LISTED_FILES = 500
#: A download is read whole while its no-follow ancestry is held.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


class HandoffFile(BaseModel):
    #: Relative to the directory, with ``/`` separators.
    path: str
    size: int
    modified_at: str


class HandoffFiles(BaseModel):
    directory: str
    files: list[HandoffFile]


def list_files() -> HandoffFiles:
    """The regular files under the directory, newest first. Links are
    neither listed nor followed."""
    root = exchange_dir()
    found: list[tuple[float, HandoffFile]] = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if not name.startswith("."))
        for name in files:
            path = Path(directory) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if name.startswith(".") or not stat.S_ISREG(info.st_mode):
                continue
            found.append(
                (
                    info.st_mtime,
                    HandoffFile(
                        path=path.relative_to(root).as_posix(),
                        size=info.st_size,
                        modified_at=datetime.fromtimestamp(info.st_mtime, UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    ),
                )
            )
    found.sort(key=lambda item: item[0], reverse=True)
    return HandoffFiles(
        directory=str(root), files=[file for _, file in found[:MAX_LISTED_FILES]]
    )


def read_file(relative: str) -> tuple[str, bytes]:
    """The name and content of one listed file."""
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative.startswith("/")
        or "\\" in relative
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
    ):
        raise AppApiError("handoff_file_not_found", status_code=404)
    path = exchange_dir().joinpath(*parts)
    try:
        if path.lstat().st_size > MAX_DOWNLOAD_BYTES:
            raise AppApiError("handoff_file_too_large", status_code=413)
        return parts[-1], read_host_file(path)
    except OSError as exc:
        raise AppApiError("handoff_file_not_found", status_code=404) from exc
