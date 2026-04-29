from __future__ import annotations

from typing import Any


def build_agent_profile(person: Any) -> dict[str, Any]:
    profile = getattr(person, "profile", {}) or {}
    profile = profile if isinstance(profile, dict) else {}
    character = profile.get("character", {}) or {}
    character = character if isinstance(character, dict) else {}
    roles = {
        role.id: _drop_empty(
            {
                "summary": role.summary,
                "description": role.description,
            }
        )
        for role in getattr(person, "roles", {}).values()
    }
    return _drop_empty(
        {
            "person_id": getattr(person, "person_id", ""),
            "name": getattr(person, "name", ""),
            "speaking_style": getattr(person, "speaking_style", ""),
            "roles": roles,
            "relationships": getattr(person, "relationships", ""),
            "character": character,
        }
    )


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _drop_empty(v)) for k, v in value.items()) if v}
    if isinstance(value, list):
        return [item for item in (_drop_empty(item) for item in value) if item]
    if isinstance(value, str):
        return value.strip()
    return value
