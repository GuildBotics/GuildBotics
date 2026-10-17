from __future__ import annotations

from pathlib import Path

from guildbotics.utils.person_id import identity_members_dir, validate_person_id

# Pure (dependency-light) avatar file helpers. Kept free of FastAPI/httpx so
# that lower layers such as the setup services can locate avatar files
# without depending on the App API module.

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def get_member_avatar_dir(person_id: str) -> Path:
    """The member's picture is part of its identity: the same in every workspace."""
    return identity_members_dir() / validate_person_id(person_id)


def find_avatar_file(person_id: str) -> Path | None:
    return avatar_in(get_member_avatar_dir(person_id))


def avatar_in(member_dir: Path) -> Path | None:
    """The avatar in a member's directory, which may be stored under a name
    that is no valid ID; ``None`` for none."""
    for ext in SUPPORTED_EXTENSIONS:
        avatar_path = member_dir / f"avatar{ext}"
        if avatar_path.is_file():
            return avatar_path
    return None
