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
from guildbotics.workspace.validation import MAX_SHARED_AVATAR_BYTES

logger = logging.getLogger("guildbotics.app_api.avatar")

# Outbound avatar downloads must time out so a stalled remote endpoint cannot
# hang the API worker indefinitely.
AVATAR_DOWNLOAD_TIMEOUT = 15.0
# An avatar is shared between the user's machines, so the size that matters is
# the one synchronization will carry. Accepting anything larger here would store
# an avatar the product displays but can never send: the sync boundary would
# hold it back on every cycle, and nothing in the normal paths would fail to
# tell the user why.
MAX_AVATAR_BYTES = MAX_SHARED_AVATAR_BYTES

__all__ = [
    "MAX_AVATAR_BYTES",
    "SUPPORTED_EXTENSIONS",
    "clean_existing_avatars",
    "download_avatar",
    "find_avatar_file",
    "get_github_avatar_url",
    "get_slack_avatar_url",
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
                path.unlink()
            except Exception as e:
                logger.warning("Failed to delete existing avatar file %s: %s", path, e)


def require_shareable_avatar(content: bytes) -> bytes:
    """Return ``content`` if it is small enough to reach the other machines.

    Both ways an avatar arrives -- uploaded, or fetched from a provider URL --
    go through here, because a check on only one of them still lets in an
    avatar that can never be shared.

    Raises:
        ValueError: When the image is above the shared size limit.
    """
    if len(content) > MAX_AVATAR_BYTES:
        raise ValueError(
            f"Avatar file is too large (max {MAX_AVATAR_BYTES // (1024 * 1024)} MB)."
        )
    return content


def store_avatar(config_dir: Path, person_id: str, content: bytes, suffix: str) -> Path:
    """Replace the member's avatar with ``content``.

    Deleting the old file and writing the new one is one change to the shared
    state: a synchronization cycle that ran between the two would commit the
    member as having no avatar. Callers hold the workspace's shared-write lock
    around this, and do their downloading outside it.
    """
    member_dir = get_member_avatar_dir(config_dir, person_id)
    member_dir.mkdir(parents=True, exist_ok=True)
    clean_existing_avatars(member_dir)
    dest_path = member_dir / f"avatar{suffix}"
    dest_path.write_bytes(content)
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


async def get_github_avatar_url(github_username: str) -> str:
    headers = {"User-Agent": "GuildBotics-App"}
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/users/{github_username}",
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

    avatar_url = data.get("avatar_url")
    if not avatar_url:
        raise ValueError(f"GitHub user '{github_username}' has no avatar URL.")
    return str(avatar_url)


async def get_slack_avatar_url(slack_user_id: str | None, slack_bot_token: str) -> str:
    headers = {"Authorization": f"Bearer {slack_bot_token}"}
    async with httpx.AsyncClient() as client:
        if not slack_user_id:
            auth_response = await client.post(
                "https://slack.com/api/auth.test",
                headers=headers,
                timeout=10.0,
            )
            auth_response.raise_for_status()
            auth_data = auth_response.json()
            if not auth_data.get("ok"):
                error = auth_data.get("error", "unknown_error")
                raise ValueError(f"Slack auth.test error: {error}")
            slack_user_id = auth_data.get("user_id")

        if not slack_user_id:
            raise ValueError("Could not resolve Slack User ID.")

        response = await client.get(
            "https://slack.com/api/users.info",
            params={"user": slack_user_id},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        raise ValueError(f"Slack API error: {error}")

    user_info = data.get("user", {})
    profile = user_info.get("profile", {})

    # Try multiple resolution sizes, fallback to default profile image
    avatar_url = (
        profile.get("image_512")
        or profile.get("image_192")
        or profile.get("image_72")
        or profile.get("image_original")
    )
    if not avatar_url:
        raise ValueError(f"Slack user '{slack_user_id}' has no avatar URL.")
    return str(avatar_url)
