"""Announce in chat that a member's ticket or pull request work waits for a
person.

A run that ends asking or blocked, or that failed and said so on its item,
leaves the member's comment as the last word on that item, so the patrol does
not take the work up again until someone answers there. The member posts one
notice in each of its chat-enabled channels so that someone learns of it. The
notice carries the stop as its status, which keeps every member from answering
it.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Literal, cast, get_args

from guildbotics.capabilities.task_runs import RunStore, TaskRunError
from guildbotics.entities.task import Task
from guildbotics.entities.team import Service
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.context import Context
from guildbotics.runtime.port import ServiceError, WorkflowStatus
from guildbotics.utils.i18n_tool import t

#: Why the work waits for a person: the member asked something, is blocked, or
#: its run failed and said so on the item.
StopReason = Literal["asking", "blocked", "failed"]
STOP_REASONS: tuple[StopReason, ...] = get_args(StopReason)

NOTIFIED_EVENT_TYPE = "work_stop.notified"


@dataclass(frozen=True)
class WorkStop:
    """One stop of a run's work, as its notice tells it.

    ``url`` is the item the work came from, ``label`` and ``title`` how the
    notice names it, and ``comment_url`` the member's comment that says why,
    when it has an address.
    """

    reason: StopReason
    run_id: str
    url: str
    label: str
    title: str
    comment_url: str = ""

    @classmethod
    def of(
        cls, task: Task, reason: StopReason, run_id: str, comment_url: str = ""
    ) -> WorkStop:
        """The stop of the run ``run_id`` on ``task``."""
        return cls(
            reason=reason,
            run_id=run_id,
            url=task.pull_request_url or task.url or "",
            label=task.label,
            title=task.title,
            comment_url=comment_url,
        )


def completed_stop(task: Task, run_id: str) -> WorkStop | None:
    """The stop the run's completion is, if it left the work waiting for a
    person; ``None`` for any other completion, or a run that recorded none."""
    store = RunStore()
    try:
        status = store.status(run_id).status
        evidence = store.evidence(run_id)
    except TaskRunError:
        return None
    if status not in STOP_REASONS:
        return None
    stop = WorkStop.of(task, cast(StopReason, status), run_id)
    return replace(stop, comment_url=comment_url_of(evidence, stop.url))


def comment_url_of(evidence: list[dict[str, Any]], url: str) -> str:
    """The address of the latest comment the run wrote on the item ``url``,
    else on any item; empty when it wrote none that has one."""
    comments = [
        item["payload"]
        for item in evidence
        if item["evidence_type"] in RunStore.TICKET_COMMENT_EVIDENCE_TYPES
        and item["payload"].get("url")
    ]
    on_item = [
        payload
        for payload in comments
        if isinstance(payload.get("target"), dict)
        and payload["target"].get("url") == url
    ]
    latest = (on_item or comments)[-1:]
    return str(latest[0]["url"]) if latest else ""


async def notify_work_stop(context: Context, stop: WorkStop) -> None:
    """Post the notice of ``stop`` as the member in each of its chat-enabled
    channels.

    It never raises: the notice cannot change how the run ended. Each channel
    it is posted to records whether it was delivered.
    """
    person = context.person
    if not context.team.project.is_available_service(Service.CHAT_SERVICE):
        return
    subscriptions = get_chat_subscriptions(person)
    if not subscriptions:
        return
    item = " ".join(part for part in (stop.label, stop.title) if part)
    lines = [
        t(
            f"capabilities.work_stops.{stop.reason}",
            name=person.name,
            item=item,
            url=stop.url,
        )
    ]
    if stop.comment_url:
        lines.append(t("capabilities.work_stops.comment", url=stop.comment_url))
    status = WorkflowStatus(
        reason=stop.reason,
        person_id=person.person_id,
        run_id=stop.run_id,
        subject=stop.url,
    )
    for subscription in subscriptions:
        channel_id = str(subscription["channel_id"])
        error = ""
        try:
            chat = context.get_chat_service()
            channel_id = channel_id or (
                await chat.resolve_channel_id(subscription["channel_name"]) or ""
            )
            if channel_id:
                await chat.post_message(channel_id, "\n".join(lines), status=status)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ServiceError) else type(exc).__name__
        with suppress(Exception):
            record_correlated_event(
                event_type=NOTIFIED_EVENT_TYPE,
                person_id=person.person_id,
                payload={
                    "run_id": stop.run_id,
                    "reason": stop.reason,
                    "url": stop.url,
                    "comment_url": stop.comment_url,
                    "channel_id": channel_id,
                    "channel_name": str(subscription["channel_name"]),
                    "delivered": bool(channel_id) and not error,
                    "error": error,
                },
            )
