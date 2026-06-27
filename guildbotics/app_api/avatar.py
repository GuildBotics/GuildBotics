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

logger = logging.getLogger("guildbotics.app_api.avatar")

# Outbound avatar downloads must time out so a stalled remote endpoint cannot
# hang the API worker indefinitely.
AVATAR_DOWNLOAD_TIMEOUT = 15.0
# Reject avatar uploads larger than this to avoid persisting huge payloads.
MAX_AVATAR_BYTES = 5 * 1024 * 1024

__all__ = [
    "MAX_AVATAR_BYTES",
    "SUPPORTED_EXTENSIONS",
    "clean_existing_avatars",
    "find_avatar_file",
    "get_github_avatar_url",
    "get_slack_avatar_url",
    "import_avatar_from_url",
    "save_avatar_file",
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


def save_avatar_file(config_dir: Path, person_id: str, upload_file: UploadFile) -> Path:
    content = upload_file.file.read()
    if len(content) > MAX_AVATAR_BYTES:
        raise ValueError(
            f"Avatar file is too large (max {MAX_AVATAR_BYTES // (1024 * 1024)} MB)."
        )

    member_dir = get_member_avatar_dir(config_dir, person_id)
    member_dir.mkdir(parents=True, exist_ok=True)

    orig_suffix = Path(upload_file.filename or "").suffix.lower()
    suffix = orig_suffix if orig_suffix in SUPPORTED_EXTENSIONS else ".png"

    clean_existing_avatars(member_dir)

    dest_path = member_dir / f"avatar{suffix}"
    with open(dest_path, "wb") as f:
        f.write(content)

    return dest_path


async def import_avatar_from_url(config_dir: Path, person_id: str, url: str) -> Path:
    member_dir = get_member_avatar_dir(config_dir, person_id)
    member_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, follow_redirects=True, timeout=AVATAR_DOWNLOAD_TIMEOUT
        )
        response.raise_for_status()

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

    clean_existing_avatars(member_dir)

    dest_path = member_dir / f"avatar{suffix}"
    dest_path.write_bytes(response.content)

    return dest_path


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
