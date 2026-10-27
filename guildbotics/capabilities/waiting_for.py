"""Validate the recipients a ticket run leaves work waiting for."""

from collections.abc import Sequence
from typing import Any

from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Service
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.port import ServiceNotFound
from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND
from guildbotics.utils.i18n_tool import t


async def validate_waiting_for(
    context: Context,
    work: Work,
    status: str,
    waiting_for: tuple[str, ...],
    evidence: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """Return unique recipients only when their patrol can receive this request.

    Read the same service snapshots and selection rules the patrol uses, but
    do not move tickets or draft PRs as a side effect of a completion check.
    """
    if not waiting_for:
        return []
    resumed = [
        item["payload"]["target"]["url"]
        for item in evidence
        if item["evidence_type"] == RunStore.CHECKS_RESUMED_EVIDENCE
        and item["payload"].get("target", {}).get("kind") == "pull_request"
    ]
    if status not in {"asking", "blocked"} or (
        not resumed
        and work.kind
        not in {
            "ticket",
            "ticket_mention",
        }
    ):
        raise MemberCapabilityError(t("capabilities.task_runs.waiting_status"))
    subject = resumed[-1] if resumed else work.identity
    members = {person.person_id: person for person in context.team.members}
    recipients = list(dict.fromkeys(waiting_for))
    for person_id in recipients:
        person = members.get(person_id)
        if (
            person is None
            or person_id == context.person.person_id
            or not person.is_active
            or TICKET_WORKFLOW_COMMAND not in person.routine_commands
        ):
            raise MemberCapabilityError(
                t("capabilities.task_runs.waiting_member", person=person_id)
            )
        other = context.clone_for(person)
        try:
            try:
                if not other.team.project.is_available_service(
                    Service.CODE_HOSTING_SERVICE
                ):
                    raise ServiceNotFound("No code host configured.")
                receives = await other.get_code_hosting_service().can_receive_work(
                    subject
                )
            except ServiceNotFound:
                receives = await other.get_ticket_service().can_receive_work(subject)
            if not receives:
                raise MemberCapabilityError(
                    t("capabilities.task_runs.waiting_item", person=person_id)
                )
        finally:
            await other.aclose()
    return recipients
