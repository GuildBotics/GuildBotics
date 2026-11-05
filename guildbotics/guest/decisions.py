"""Durable judgment snapshots cross the command's authenticated host window."""

from datetime import datetime
from typing import Any

from guildbotics.guest.host_client import HostClient


class JudgmentIncomplete(RuntimeError):
    """A fast path could not prove it completed; keep the input pending."""


async def judge_chat(context: Any, turn: Any) -> bool:
    """Judge inside the turn's VM, then use the same member publication gate.

    Return true only when the workflow should invoke its response agent.
    Every fast path rechecks the queue and provider snapshot before acting.
    """
    from guildbotics.guest.host_client import command_window
    from guildbotics.guest.window import _member_result
    from guildbotics.intelligences.decisions.assessment import assess
    from guildbotics.intelligences.effort import promote_effort

    client = command_window()
    if client is None:
        return True
    from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY

    invocation = getattr(context, "shared_state", {}).get(WORKFLOW_INVOCATION_KEY)
    person = context.person.person_id
    state = {
        **{
            key: turn.prompt[key]
            for key in (
                "unprocessed_messages",
                "participant_labels",
                "handoff_candidates",
                "chat_participation",
                "previous_thread_context",
                "thread_context",
                "thread_context_complete",
            )
            if key in turn.prompt
        },
        "member": {
            "person_id": person,
            "roles": {
                role.id: {
                    key: value
                    for key, value in (
                        ("summary", role.summary.strip()),
                        ("description", role.description.strip()),
                    )
                    if value
                }
                for role in context.person.roles.values()
            },
            "profile": context.person.profile,
        },
        "service": turn.subject.service,
        "channel_id": turn.subject.channel_id,
        "thread_id": turn.subject.thread_id,
        "event_ids": turn.prompt.get("event_ids", [turn.subject.event_id]),
        "reaction_target": turn.message_id,
        "run_id": getattr(invocation, "run_id", ""),
        "previous_outcomes": client.call("evidence"),
    }
    selection, _ = await assess(
        state,
        None,
        person_id=person,
        logger=context.logger,
        brain_factory=context.brain_factory,
        recorder=DecisionRecorder(client, adopt_effort=True),
    )
    if selection.route == "agent":
        turn.effort = promote_effort(turn.effort, selection.response_effort)
        return True

    async def member(*arguments: str, content: str = "") -> dict[str, Any]:
        return await _member_result(client, [*arguments, "--person", person], content)

    async def current() -> None:
        updates = await member("chat", "updates")
        if updates.get("status") != "up_to_date":
            raise JudgmentIncomplete("Chat input changed or reception is unavailable.")

    try:
        await current()
        from guildbotics.runtime.chat_service import message_position

        messages: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            snapshot = await member(
                "chat",
                "inspect",
                "thread",
                "--channel-id",
                turn.subject.channel_id,
                "--thread-id",
                turn.subject.thread_id,
                "--limit",
                "100",
                *(["--cursor", cursor] if cursor else []),
            )
            messages.extend(snapshot["messages"])
            if source_cursor := turn.prompt.get("source_cursor"):
                indexed = {
                    message_position(
                        datetime.fromisoformat(m["occurred_at"]), m["message_id"]
                    ): m
                    for m in messages
                }
                previous = sorted(key for key in indexed if key < source_cursor)
                for key in previous[:-100]:
                    del indexed[key]
                messages = [indexed[key] for key in sorted(indexed)]
            cursor = snapshot.get("next_cursor", "")
            if not cursor:
                break
            if cursor in seen:
                raise JudgmentIncomplete("The provider repeated a thread page.")
            seen.add(cursor)
        if (
            "source_snapshot" not in turn.prompt
            or messages != turn.prompt["source_snapshot"]
        ):
            raise JudgmentIncomplete("The provider thread changed during judgment.")
        await current()
        if selection.route == "reaction-only":
            if selection.reaction is None:
                raise JudgmentIncomplete(
                    "The reaction judgment has no target reaction."
                )
            await member(
                "chat",
                "reaction",
                "add",
                "--channel-id",
                turn.subject.channel_id,
                "--message-id",
                turn.message_id,
                "--reaction",
                selection.reaction,
            )
        else:
            await member("chat", "noop", "--content-stdin", content=selection.reason)
        await current()
        await member(
            "chat",
            "complete",
            "--status",
            "done",
            "--content-stdin",
            content=selection.reason,
        )
        client.call("require_completion")
        client.call("record_completed", attempt=1)
    except JudgmentIncomplete:
        raise
    except Exception as error:
        raise JudgmentIncomplete("The chat judgment could not be confirmed.") from error
    return False


class DecisionRecorder:
    def __init__(self, client: HostClient, *, adopt_effort: bool = False) -> None:
        self.client = client
        self.adopt_effort = adopt_effort

    def io(self, record_id: str, payload: dict[str, Any]) -> None:
        if self.adopt_effort and "selection" in payload:
            payload = {**payload, "adopt_response_effort": True}
        self.client.call("required_io", record_id=record_id, payload=payload)

    def evaluated(self, person_id: str, payload: dict[str, Any]) -> None:
        self.client.call("decision_evaluated", payload=payload)
