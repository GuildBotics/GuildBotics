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


def build_member_communication_style(person: Any) -> dict[str, str]:
    profile = build_agent_profile(person)
    name = str(profile.get("name") or getattr(person, "name", "") or "the member")
    person_id = str(
        profile.get("person_id") or getattr(person, "person_id", "") or "unknown"
    )
    voice_basis = _voice_basis(profile)
    if not voice_basis:
        voice_basis = (
            "Use the member's configured profile and roles as the source of truth."
        )

    return {
        "voice_basis": voice_basis,
        "active_member_instruction": (
            f"Treat {name} (person_id: {person_id}) as the active GuildBotics "
            "member for this session until the user explicitly switches or clears "
            "the member. Do not ask the user to repeat the person id while this "
            "active member is established."
        ),
        "interactive_replies": (
            f"Write interactive replies to the user as {name}. Use the user's "
            "language, reflect the voice_basis lightly, and keep technical details "
            "accurate and actionable. Do not turn this into exaggerated roleplay."
        ),
        "github_comments": (
            f"Write GitHub issue comments, PR conversation comments, and PR review "
            f"thread replies in {name}'s natural conversational voice, while staying "
            "clear about technical facts and next actions."
        ),
        "neutral_documents": (
            "Use the project's neutral document style for issue titles/bodies, PR "
            "titles/bodies, commit messages, and durable task summaries. The member's "
            "judgment may shape the content, but the prose should remain document-like."
        ),
        "machine_outputs": (
            "Do not apply the member voice to command output, command arguments, IDs, "
            "paths, logs, machine-readable JSON, workflow AgentResponse.message, or "
            "other control data. Keep those values factual and valid."
        ),
    }


def _voice_basis(profile: dict[str, Any]) -> str:
    parts: list[str] = []
    speaking_style = profile.get("speaking_style")
    if isinstance(speaking_style, str) and speaking_style.strip():
        parts.append(f"Explicit speaking style: {speaking_style.strip()}")

    character = profile.get("character", {})
    if isinstance(character, dict):
        archetype = character.get("archetype")
        if isinstance(archetype, str) and archetype.strip():
            parts.append(f"Character archetype: {archetype.strip()}")
        traits = _string_list(character.get("traits"))
        if traits:
            parts.append(f"Traits to reflect lightly: {', '.join(traits)}")
        interests = _string_list(character.get("interests"))
        if interests:
            parts.append(
                f"Interests that can shape perspective: {', '.join(interests)}"
            )
        preferences = character.get("conversation_preferences")
        if isinstance(preferences, dict):
            contribution_style = _string_list(preferences.get("contribution_style"))
            if contribution_style:
                parts.append(
                    f"Conversation contribution style: {', '.join(contribution_style)}"
                )

    roles = profile.get("roles", {})
    if isinstance(roles, dict):
        role_summaries = [
            str(role.get("summary", "")).strip()
            for role in roles.values()
            if isinstance(role, dict) and str(role.get("summary", "")).strip()
        ]
        if role_summaries:
            parts.append(f"Role perspective: {', '.join(role_summaries)}")

    return "\n".join(f"- {part}" for part in parts)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _drop_empty(v)) for k, v in value.items()) if v}
    if isinstance(value, list):
        return [item for item in (_drop_empty(item) for item in value) if item]
    if isinstance(value, str):
        return value.strip()
    return value
