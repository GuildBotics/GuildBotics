from __future__ import annotations

from pathlib import Path

# Pure (dependency-light) avatar file helpers. Kept free of FastAPI/httpx so
# that lower layers such as the edition/setup services can locate avatar files
# without depending on the App API module.

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def get_member_avatar_dir(config_dir: Path, person_id: str) -> Path:
    return config_dir / "team" / "members" / person_id


def find_avatar_file(config_dir: Path, person_id: str) -> Path | None:
    member_dir = get_member_avatar_dir(config_dir, person_id)
    if not member_dir.exists():
        return None
    for ext in SUPPORTED_EXTENSIONS:
        avatar_path = member_dir / f"avatar{ext}"
        if avatar_path.is_file():
            return avatar_path
    return None
