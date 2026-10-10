"""What ``member context`` tells a member about itself and its work."""

from __future__ import annotations

from typing import Any

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.entities.team import Service
from guildbotics.runtime.context import Context
from guildbotics.utils.person_profile import build_member_communication_style


async def member_context(
    context: Context, check_credentials: bool = False
) -> dict[str, Any]:
    """The member's non-secret context and the capability reference.

    The code host adds how it knows the member, and with
    ``check_credentials`` confirms that it accepts the member's credential.
    """
    person = context.person
    identity: dict[str, str] = {}
    credential_status = "unchecked"
    if context.team.project.is_available_service(Service.CODE_HOSTING_SERVICE):
        identity = await context.get_code_hosting_service().identity(
            check=check_credentials
        )
        if check_credentials:
            credential_status = "ok"
    return {
        "person_id": person.person_id,
        "name": person.name,
        "person_type": person.person_type,
        "is_active": person.is_active,
        "roles": {
            role_id: {"summary": role.summary, "description": role.description}
            for role_id, role in person.roles.items()
        },
        "profile": person.profile,
        "speaking_style": person.speaking_style,
        "communication_style": build_member_communication_style(person),
        **identity,
        "credential_status": credential_status,
        "memory": MemberMemoryService(person).load_context_memory(),
        # The full member command surface and cross-cutting rules. This is
        # the same reference printed by ``guildbotics member help`` and is
        # the single source every entrypoint relies on (context is the
        # mandatory first call), so each member can perform code-host, git,
        # and chat work regardless of which workflow invoked it. Task
        # contracts (primary objective, required completion command) stay in
        # the prompts, never here.
        "capabilities": capability_reference_text(),
    }
