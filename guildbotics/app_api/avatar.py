from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import UploadFile

from guildbotics.utils.avatar import (
    SUPPORTED_EXTENSIONS,
    find_avatar_file,
    get_member_avatar_dir,
)
from guildbotics.utils.shared_writes import delete_shared_path, write_shared_bytes

logger = logging.getLogger("guildbotics.app_api.avatar")

# Outbound avatar downloads must time out so a stalled remote endpoint cannot
# hang the API worker indefinitely.
AVATAR_DOWNLOAD_TIMEOUT = 15.0
# Every path that stores an avatar -- an upload and a URL import -- is held to
# this one bound.
MAX_AVATAR_BYTES = 4_194_304

__all__ = [
    "MAX_AVATAR_BYTES",
    "SUPPORTED_EXTENSIONS",
    "clean_existing_avatars",
    "download_avatar",
    "find_avatar_file",
    "read_upload",
    "require_shareable_avatar",
    "store_avatar",
]


def clean_existing_avatars(member_dir: Path) -> None:
    if not member_dir.exists():
        return
    for path in member_dir.iterdir():
        if (
            path.is_file()
            and path.stem == "avatar"
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ):
            try:
                delete_shared_path(path)
            except Exception as e:
                logger.warning("Failed to delete existing avatar file %s: %s", path, e)


def require_shareable_avatar(content: bytes) -> bytes:
    """Return ``content`` if it is within the avatar size limit.

    Both ways an avatar arrives -- uploaded, or fetched from a provider URL --
    go through here, because a check on only one of them still lets in an
    oversized avatar.

    Raises:
        ValueError: When the image is above the size limit.
    """
    if len(content) > MAX_AVATAR_BYTES:
        raise ValueError(
            f"Avatar file is too large (max {MAX_AVATAR_BYTES // (1024 * 1024)} MB)."
        )
    return content


def store_avatar(person_id: str, content: bytes, suffix: str) -> Path:
    """Replace the member's avatar with ``content``.

    Deleting the old file and writing the new one is one change to the shared
    state: a reader between the two would see the member as having no avatar.
    Callers hold the identities' shared-write lock
    around this, and do their downloading outside it.
    """
    member_dir = get_member_avatar_dir(person_id)
    member_dir.mkdir(parents=True, exist_ok=True)
    clean_existing_avatars(member_dir)
    dest_path = member_dir / f"avatar{suffix}"
    write_shared_bytes(dest_path, content)
    return dest_path


def read_upload(upload_file: UploadFile) -> tuple[bytes, str]:
    """Return an uploaded avatar's content and the suffix to store it under."""
    content = require_shareable_avatar(upload_file.file.read())
    orig_suffix = Path(upload_file.filename or "").suffix.lower()
    return content, orig_suffix if orig_suffix in SUPPORTED_EXTENSIONS else ".png"


async def download_avatar(url: str) -> tuple[bytes, str]:
    """Fetch an avatar and return its content and the suffix to store it under.

    Downloading is kept apart from storing so the wait on a remote server
    happens outside the workspace's shared-write lock.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, follow_redirects=True, timeout=AVATAR_DOWNLOAD_TIMEOUT
        )
        response.raise_for_status()

    content = require_shareable_avatar(response.content)
    content_type = response.headers.get("Content-Type", "").lower()
    if "png" in content_type:
        suffix = ".png"
    elif "jpeg" in content_type or "jpg" in content_type:
        suffix = ".jpg"
    elif "gif" in content_type:
        suffix = ".gif"
    elif "webp" in content_type:
        suffix = ".webp"
    else:
        # Fallback to suffix from url or default png
        url_suffix = Path(url.split("?", maxsplit=1)[0]).suffix.lower()
        suffix = url_suffix if url_suffix in SUPPORTED_EXTENSIONS else ".png"

    return content, suffix
