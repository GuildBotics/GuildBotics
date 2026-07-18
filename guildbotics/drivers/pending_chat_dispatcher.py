from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from guildbotics.capabilities.workflow_rate_limits import (
    workflow_rate_limit_from_exception,
)
from guildbotics.drivers.execution import ExecutionCoordinator, WorkRejectedError
from guildbotics.drivers.workflow_dispatcher import WorkflowDispatcher
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    PendingChatEvent,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability import trace_scope
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
        execution_coordinator: ExecutionCoordinator | None = None,
    ) -> None:
        self._context = context
        self._workflow_command = workflow_command
        self._state_store = state_store or FileConversationStateStore()
        self._service_run_id = service_run_id
        self._execution = execution_coordinator or ExecutionCoordinator()

    async def process_person(
        self, person: Person, stop_event: threading.Event | None = None
    ) -> int:
        """Drain and process every queued chat event for one member.

        Returns the number of events processed. Events are handled FIFO per
        Slack thread: while a thread's oldest event is waiting for retry or
        fails again, later events of the same thread are not dispatched, so a
        follow-up message can never advance the shared provider conversation
        past a still-pending earlier event. Other threads (and other members)
        keep draining independently. A newer message arriving in a blocked
        thread wakes the waiting head early instead of being run itself.
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
                key=lambda pending: _message_order_key(pending.event.message_ts),
            )
            for thread_events in _thread_groups(pending_events):
                for pending in thread_events:
                    if stop_event is not None and stop_event.is_set():
                        return processed
                    if not self._process_one(person, service, channel_id, pending):
                        continue
                    if not _is_due(pending) and not self._wake_for_follower(
                        service, person.person_id, channel_id, pending, thread_events
                    ):
                        break
                    if not await self._dispatch(person, service, channel_id, pending):
                        break
                    processed += 1
        return processed

    def _wake_for_follower(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        head: PendingChatEvent,
        thread_events: list[PendingChatEvent],
    ) -> bool:
        """Retry a backing-off thread head early when a newer message arrived.

        A provider-imposed rate limit is always waited out, and each follower
        cursor wakes the head at most once (persisted, so a restart cannot turn
        the same follow-up message into an unlimited retry source).
        """
        if head.last_error_category == "rate_limited":
            return False
        follower_cursor = max(
            (
                pending.event.message_ts
                for pending in thread_events
                if pending is not head
            ),
            key=_message_order_key,
            default="",
        )
        if not follower_cursor or _message_order_key(
            follower_cursor
        ) <= _message_order_key(head.wake_cursor):
            return False
        head.wake_cursor = follower_cursor
        head.next_attempt_at = None
        self._state_store.save_pending_event(service, person_id, channel_id, head)
        return True

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
        with trace_scope(
            "event_listener",
            person_id=person.person_id,
            command=self._workflow_command,
        ):
            try:
                with self._execution.track_work(
                    source="event_queue",
                    person_id=person.person_id,
                    command=self._workflow_command,
                ):
                    # Consume a retry attempt only once the work is accepted, so a
                    # dispatch rejected while the runtime drains does not burn the
                    # event's retry budget without ever running the workflow.
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
                    await self._run_workflow(person, service, channel_id, pending)
            except WorkRejectedError:
                return 0
            except Exception as exc:  # leave queued for retry; do not block the queue
                rate_limit = workflow_rate_limit_from_exception(exc)
                pending.last_error_category = (
                    "rate_limited" if rate_limit is not None else "failed"
                )
                if pending.attempt_count >= pending.max_attempts:
                    # The workflow normally terminalizes its own final attempt;
                    # reaching here means it could not. Release the thread so
                    # the abandoned event never blocks its followers.
                    self._context.logger.error(
                        "chat event abandoned after final attempt: "
                        "person=%s channel=%s event=%s attempt=%s/%s error=%s",
                        person.person_id,
                        channel_id,
                        event_id,
                        pending.attempt_count,
                        pending.max_attempts,
                        exc,
                    )
                    self._state_store.mark_processed_event(
                        service, person.person_id, channel_id, event_id
                    )
                    self._state_store.remove_pending_event(
                        service, person.person_id, channel_id, event_id
                    )
                    return 0
                pending.next_attempt_at = (
                    rate_limit.retry_after_at
                    if rate_limit is not None and rate_limit.retry_after_at
                    else _next_attempt_at(pending.attempt_count)
                )
                self._state_store.save_pending_event(
                    service, person.person_id, channel_id, pending
                )
                self._context.logger.warning(
                    "chat event processing failed: person=%s channel=%s event=%s "
                    "attempt=%s/%s error=%s",
                    person.person_id,
                    channel_id,
                    event_id,
                    pending.attempt_count,
                    pending.max_attempts,
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


def _thread_groups(events: list[PendingChatEvent]) -> list[list[PendingChatEvent]]:
    """Group message-ts-ordered events by thread, oldest thread first."""
    groups: dict[str, list[PendingChatEvent]] = {}
    for pending in events:
        key = pending.event.thread_ts or pending.event.message_ts
        groups.setdefault(key, []).append(pending)
    return list(groups.values())


def _message_order_key(message_ts: str) -> tuple[int, ...]:
    """Numeric ordering key for Slack-style timestamps; unparsable sorts first."""
    try:
        return tuple(int(part) for part in message_ts.split("."))
    except ValueError:
        return ()


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
