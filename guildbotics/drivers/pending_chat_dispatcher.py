from __future__ import annotations

import threading

from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    PendingChatEvent,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability import trace_scope
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)


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
                if self._process_one(person, service, channel_id, pending):
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
        try:
            await self._run_workflow(person, service, channel_id, pending)
        except Exception as exc:  # leave queued for retry; do not block the queue
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
        with trace_scope(
            "event_listener",
            person_id=person.person_id,
            command=self._workflow_command,
            attributes={
                "service_run_id": self._service_run_id,
                "event.provider": service,
                "slack.channel": channel_id,
                "slack.thread_ts": pending.event.thread_ts,
                "slack.ts": pending.event.message_ts,
                "event_id": pending.event.event_id,
            },
        ):
            context = self._context.clone_for(person)
            context.shared_state[INCOMING_CHAT_EVENT_KEY] = incoming.to_shared_state()
            try:
                await CommandRunner(context, self._workflow_command, []).run()
            finally:
                await context.aclose()
