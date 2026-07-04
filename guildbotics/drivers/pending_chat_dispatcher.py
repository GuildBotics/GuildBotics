from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from guildbotics.drivers.workflow_dispatcher import WorkflowDispatcher
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    PendingChatEvent,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import IncomingChatEvent
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.timestamps import parse_iso_datetime

_SECOND_ATTEMPT = 2
_THIRD_ATTEMPT = 3
_CHAT_MAX_ATTEMPTS_ENV = "GUILDBOTICS_CHAT_MAX_ATTEMPTS"
_DEFAULT_MAX_ATTEMPTS = 5


class PendingChatDispatcher:
    """Runs the chat workflow for queued chat events inside the member worker.

    The event listener only receives, backfills, and queues chat events. Actual
    execution happens here, called from each member's single scheduler worker
    thread, so a member's chat / ticket / scheduled / routine work shares one
    serial queue and never runs two agents in the same workspace at once.
    """

    def __init__(
        self,
        context: Context,
        workflow_command: str = "workflows/chat_conversation_workflow",
        state_store: ConversationStateStore | None = None,
        service_run_id: str | None = None,
    ) -> None:
        self._context = context
        self._workflow_command = workflow_command
        self._state_store = state_store or FileConversationStateStore()
        self._service_run_id = service_run_id

    async def process_person(
        self, person: Person, stop_event: threading.Event | None = None
    ) -> int:
        """Drain and process every queued chat event for one member.

        Returns the number of events processed. A failing event is left queued
        (and logged) so it is retried on a later pass instead of blocking the
        rest of the queue.
        """
        processed = 0
        for service, channel_id in self._state_store.list_pending_channels(
            person.person_id
        ):
            if stop_event is not None and stop_event.is_set():
                break
            pending_events = sorted(
                self._state_store.load_pending_events(
                    service, person.person_id, channel_id
                ),
                key=lambda pending: pending.event.message_ts,
            )
            for pending in pending_events:
                if stop_event is not None and stop_event.is_set():
                    break
                if self._process_one(person, service, channel_id, pending) and _is_due(
                    pending
                ):
                    processed += await self._dispatch(
                        person, service, channel_id, pending
                    )
        return processed

    def _process_one(
        self,
        person: Person,
        service: str,
        channel_id: str,
        pending: PendingChatEvent,
    ) -> bool:
        """Skip and clean up an event already marked processed; else signal run."""
        event_id = pending.event.event_id
        if self._state_store.is_processed_event(
            service, person.person_id, channel_id, event_id
        ):
            self._state_store.remove_pending_event(
                service, person.person_id, channel_id, event_id
            )
            return False
        return True

    async def _dispatch(
        self,
        person: Person,
        service: str,
        channel_id: str,
        pending: PendingChatEvent,
    ) -> int:
        event_id = pending.event.event_id
        if not pending.run_id:
            pending.run_id = uuid4().hex
        if pending.attempt_count <= 0:
            pending.max_attempts = _max_pending_attempts()
        pending.attempt_count = max(0, pending.attempt_count) + 1
        pending.max_attempts = max(1, pending.max_attempts)
        pending.next_attempt_at = None
        self._state_store.save_pending_event(
            service, person.person_id, channel_id, pending
        )
        try:
            await self._run_workflow(person, service, channel_id, pending)
        except Exception as exc:  # leave queued for retry; do not block the queue
            pending.next_attempt_at = _next_attempt_at(pending.attempt_count)
            self._state_store.save_pending_event(
                service, person.person_id, channel_id, pending
            )
            self._context.logger.warning(
                "chat event processing failed: person=%s channel=%s event=%s error=%s",
                person.person_id,
                channel_id,
                event_id,
                exc,
            )
            return 0
        self._state_store.mark_processed_event(
            service, person.person_id, channel_id, event_id
        )
        self._state_store.remove_pending_event(
            service, person.person_id, channel_id, event_id
        )
        return 1

    async def _run_workflow(
        self,
        person: Person,
        service: str,
        channel_id: str,
        pending: PendingChatEvent,
    ) -> None:
        incoming = IncomingChatEvent(
            service_name=service,
            channel_id=channel_id,
            event=pending.event,
            chat_participation=pending.chat_participation,
        )
        invocation = WorkflowInvocation(
            command=self._workflow_command,
            person_id=person.person_id,
            source="event_queue",
            trigger_type="chat",
            payload={
                **incoming.to_shared_state(),
                "retry_context": {
                    "attempt_count": pending.attempt_count,
                    "max_attempts": pending.max_attempts,
                    "is_final_attempt": pending.attempt_count >= pending.max_attempts,
                    "run_id": pending.run_id,
                },
            },
            idempotency_key=f"{service}:message:{channel_id}:{pending.event.event_id}",
        )
        dispatcher = WorkflowDispatcher(
            self._context, service_run_id=self._service_run_id
        )
        await dispatcher.dispatch(invocation, person)


def _is_due(pending: PendingChatEvent) -> bool:
    if not pending.next_attempt_at:
        return True
    parsed = parse_iso_datetime(pending.next_attempt_at)
    if parsed is None:
        return True
    return parsed.astimezone(UTC) <= datetime.now(UTC)


def _next_attempt_at(attempt_count: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=_backoff_seconds(attempt_count))
    ).isoformat()


def _backoff_seconds(attempt_count: int) -> int:
    if attempt_count <= 1:
        return 60
    if attempt_count == _SECOND_ATTEMPT:
        return 120
    if attempt_count == _THIRD_ATTEMPT:
        return 300
    return 900


def _max_pending_attempts() -> int:
    raw = os.getenv(_CHAT_MAX_ATTEMPTS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS
