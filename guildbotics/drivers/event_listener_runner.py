from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from guildbotics.entities.team import Person
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus, ReceiveState
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ThreadConversationState,
)
from guildbotics.integrations.chat_workflow_status import is_suppressed_chat_event
from guildbotics.integrations.event_listener import EventListener
from guildbotics.integrations.factory import (
    ServiceIntegrationFactory,
    chat_provider_name,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.chat_service import (
    ChatEvent,
    ChatService,
    ChatServiceError,
    ChatThreadNotFoundError,
)
from guildbotics.runtime.context import Context
from guildbotics.utils.shared_write_lock import SharedWriteBusyError, shared_write_lock

SubscriptionSignature = tuple[tuple[tuple[str, str], ...], ...]
ResolvedSubscriptions = dict[str, "ChatBackfillPolicy"]


@dataclass(frozen=True, slots=True)
class ChatBackfillPolicy:
    startup_minutes: int = 60
    interval_seconds: float = 300.0
    overlap_seconds: float = 60.0
    limit: int = 100
    participation: str = "strict"


class EventListenerRunner:
    """Run event-driven chat workflows in a dedicated worker thread."""

    def __init__(
        self,
        context: Context,
        poll_interval_seconds: float = 5.0,
        service_run_id: str | None = None,
        state_store: ConversationStateStore | None = None,
        startup_backfill_minutes: int = 60,
        backfill_interval_seconds: float = 300.0,
        on_stopped: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self.service_run_id = service_run_id
        self._on_stopped = on_stopped
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._default_backfill_policy = ChatBackfillPolicy(
            startup_minutes=max(0, int(startup_backfill_minutes)),
            interval_seconds=max(0.0, float(backfill_interval_seconds)),
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        # Set inside the worker thread so stop() can cancel an in-flight cycle.
        # The cycle only drains/backfills, but backfill awaits provider requests
        # that the stop event cannot interrupt; cancelling the cycle aborts those
        # awaits so a stop overlapping a backfill does not exceed the stop timeout.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_cycle: asyncio.Task[None] | None = None
        self._receive_wakeup = asyncio.Event()
        self._receive_status = ChatReceiveStatus()
        self._backfill_waiting: set[tuple[str, str, str]] = set()
        self._factory = ServiceIntegrationFactory()
        #: The provider the project's chat is, which keys what is recorded.
        self._service = ""
        self._connection_subscriptions: dict[
            str, list[tuple[Person, ResolvedSubscriptions]]
        ] = {}
        self._undelivered: dict[str, list[ChatEvent]] = {}
        self._listeners: dict[str, EventListener] = {}
        self._subscription_channel_cache: dict[
            str, tuple[SubscriptionSignature, ResolvedSubscriptions]
        ] = {}
        self._last_group_log_state: tuple[int, int] | None = None
        self._last_backfill_at: dict[tuple[str, str, str], float] = {}
        self._startup_backfilled: set[tuple[str, str, str]] = set()
        self._cycle_count = 0
        self._cycle_failure_count = 0
        self._events_drained_count = 0
        self._events_pending_count = 0
        self._events_backfilled_count = 0
        self._state_store = state_store or FileConversationStateStore()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="guildbotics-event-listener-runner",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # Cancel an in-flight cycle so a stop overlapping a backfill aborts the
        # awaited provider request instead of waiting it out past the stop timeout.
        # Scheduled onto the worker loop because stop() runs on another thread.
        loop = self._loop
        cycle = self._active_cycle
        if loop is not None and cycle is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(cycle.cancel)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def get_status_summary(self) -> dict[str, Any]:
        """Return lightweight runtime counters for GUI status displays."""
        subscription_count = sum(
            len(channel_ids)
            for _, channel_ids in self._subscription_channel_cache.values()
        )
        auth_failed_persons: list[str] = []
        auth_failed_count = 0
        for key, listener in self._listeners.items():
            if listener.auth_failed:
                auth_failed_count += 1
                auth_failed_persons.extend(
                    person.person_id
                    for person, _ in self._connection_subscriptions.get(key, [])
                )
        return {
            "subscription_count": subscription_count,
            "listener_count": len(self._listeners),
            "cycle_count": self._cycle_count,
            "cycle_failure_count": self._cycle_failure_count,
            "events_drained_count": self._events_drained_count,
            "events_pending_count": self._events_pending_count,
            "events_backfilled_count": self._events_backfilled_count,
            "events_auth_failed_count": auth_failed_count,
            "events_auth_failed_persons": sorted(set(auth_failed_persons)),
        }

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run_loop())
        finally:
            self._loop = None
            try:
                loop.run_until_complete(self.context.aclose())
            finally:
                loop.close()
                if self._on_stopped is not None:
                    self._on_stopped()

    async def _run_loop(self) -> None:
        # A stopped runner can restart on a new event loop.
        self._receive_wakeup = asyncio.Event()
        receiver = asyncio.create_task(self._receive_loop())
        try:
            await self._backfill_loop()
        finally:
            receiver.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await receiver
            finally:
                await self._aclose_sources()

    def _wake_receiver(self) -> None:
        loop = self._loop
        if loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._receive_wakeup.set)

    async def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            self._receive_wakeup.clear()
            for key in list(self._connection_subscriptions):
                try:
                    self._flush_listener(key)
                except OSError as exc:
                    self._log_warning("chat receiver heartbeat failed: %s", exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._receive_wakeup.wait(), timeout=1.0)

    async def _backfill_loop(self) -> None:
        while not self._stop_event.is_set():
            self._cycle_count += 1
            try:
                self._active_cycle = asyncio.ensure_future(self._run_once())
                await self._active_cycle
            except asyncio.CancelledError:
                # stop() cancelled the in-flight cycle; exit promptly.
                break
            except Exception as exc:  # pragma: no cover - defensive worker guard
                self._cycle_failure_count += 1
                self._log_warning("event runner cycle failed: %s", exc)
            finally:
                self._active_cycle = None
            await asyncio.sleep(self.poll_interval_seconds)
        self._log_info(
            "event listener runner summary: cycles=%d cycle_failures=%d drained=%d "
            "pending=%d backfilled=%d",
            self._cycle_count,
            self._cycle_failure_count,
            self._events_drained_count,
            self._events_pending_count,
            self._events_backfilled_count,
        )

    async def _run_once(self) -> None:
        await self._drain_backfill_and_queue()

    async def _drain_backfill_and_queue(self) -> None:
        grouped = await self._build_person_subscriptions_by_connection()
        if grouped:
            group_log_state = (len(grouped), len(self._listeners))
            if self._last_group_log_state != group_log_state:
                self._last_group_log_state = group_log_state
                self._log_info(
                    "event listener runner: %d shared listener group(s), %d active listener(s) cached",
                    len(grouped),
                    len(self._listeners),
                )
        elif self._last_group_log_state is not None:
            self._last_group_log_state = None
            self._log_info(
                "event listener runner: no active event listener subscriptions"
            )
        for key, subscribers in self._connection_subscriptions.items():
            for person, channels in subscribers:
                for channel_id in channels:
                    if not any(
                        p.person_id == person.person_id and channel_id in c
                        for p, c in grouped.get(key, [])
                    ):
                        self._receive_status.save(
                            self._service,
                            person.person_id,
                            channel_id,
                            state="unavailable",
                        )
        self._connection_subscriptions = grouped
        for key, person_subs in grouped.items():
            if self._stop_event.is_set():
                break
            self._get_or_create_listener(key, person_subs).start()
            self._flush_listener(key)
            backfilled = await asyncio.gather(
                *(
                    self._backfill_person(person, subscriptions)
                    for person, subscriptions in person_subs
                )
            )
            self._events_backfilled_count += sum(backfilled)

    def _flush_listener(self, key: str) -> None:
        listener = self._listeners.get(key)
        if listener is None:
            return
        subscribers = self._connection_subscriptions.get(key, [])
        pending = self._undelivered.setdefault(key, [])
        state: ReceiveState = "ready"
        try:
            drained = listener.drain_events()
            self._events_drained_count += len(drained)
            pending.extend(drained)
            # Never block the event loop behind sync. Re-entrant writes below
            # inherit this lock, and the receive loop retries on its next wake.
            if pending:
                with shared_write_lock(timeout=0):
                    while pending:
                        event = pending[0]
                        for person, subscriptions in subscribers:
                            if event.channel_id in subscriptions:
                                self._events_pending_count += self._queue_event(
                                    person,
                                    event,
                                    subscriptions[event.channel_id].participation,
                                )
                        pending.pop(0)
        except SharedWriteBusyError:
            state = "catching_up" if pending else "ready"
        except Exception as exc:
            state = "unavailable"
            self._log_warning("chat receive persistence failed: %s", exc)
        if not listener.connected or self._stop_event.is_set():
            state = "unavailable"
        for person, channels in subscribers:
            for channel_id in channels:
                scope = (self._service, person.person_id, channel_id)
                channel_state = (
                    "catching_up"
                    if state == "ready" and scope in self._backfill_waiting
                    else state
                )
                self._receive_status.save(*scope, state=channel_state)

    async def _write_backfill[T](
        self, scope: tuple[str, str, str], write: Callable[[], T]
    ) -> T:
        """Wait for sync cooperatively so socket reception and heartbeats continue."""
        self._backfill_waiting.add(scope)
        try:
            while not self._stop_event.is_set():
                try:
                    with shared_write_lock(timeout=0):
                        return write()
                except SharedWriteBusyError:
                    if self._receive_status.state(*scope) == "ready":
                        self._receive_status.save(*scope, state="catching_up")
                    await asyncio.sleep(1.0)
            raise asyncio.CancelledError
        finally:
            self._backfill_waiting.discard(scope)
            self._wake_receiver()

    async def _backfill_person(
        self, person: Person, subscriptions: ResolvedSubscriptions
    ) -> int:
        """Backfill one member's channels into the pending queue (no execution)."""
        backfilled = 0
        for channel_id, policy in subscriptions.items():
            if self._stop_event.is_set():
                break
            backfilled += await self._backfill_due_events(person, channel_id, policy)
        return backfilled

    async def _build_person_subscriptions_by_connection(
        self,
    ) -> dict[str, list[tuple[Person, ResolvedSubscriptions]]]:
        grouped: dict[str, list[tuple[Person, ResolvedSubscriptions]]] = {}
        team = self.context.team
        try:
            self._service = chat_provider_name(team)
        except ChatServiceError:
            return grouped
        for person in team.members:
            if self._stop_event.is_set():
                break
            if not getattr(person, "is_active", False):
                continue
            subscriptions = get_chat_subscriptions(person)
            if not subscriptions:
                continue
            resolved = await self._resolve_subscriptions_cached(person, subscriptions)
            if not resolved:
                continue
            try:
                key = self._factory.listener_key(person, team)
            except ChatServiceError as e:
                self._log_warning(
                    "event listener runner skipped person=%s: %s", person.person_id, e
                )
                continue
            grouped.setdefault(key, []).append((person, resolved))
        return grouped

    async def _resolve_subscriptions_cached(
        self,
        person: Person,
        subscriptions: list[dict[str, Any]],
    ) -> ResolvedSubscriptions:
        signature = self._subscription_signature(subscriptions)
        cached = self._subscription_channel_cache.get(person.person_id)
        if cached is not None and cached[0] == signature:
            return dict(cached[1])

        resolved_subs = await self._resolve_subscription_channels(person, subscriptions)
        if not resolved_subs:
            self._subscription_channel_cache.pop(person.person_id, None)
            return {}

        resolved = {
            channel_id: self._backfill_policy_from_subscription(sub)
            for sub in resolved_subs
            if (channel_id := str(sub.get("channel_id", "")).strip())
        }
        if not resolved:
            self._subscription_channel_cache.pop(person.person_id, None)
            return {}

        self._subscription_channel_cache[person.person_id] = (
            signature,
            dict(resolved),
        )
        return resolved

    def _subscription_signature(
        self, subscriptions: list[dict[str, Any]]
    ) -> SubscriptionSignature:
        # Signature uses fields that affect channel resolution/routing so config changes
        # trigger re-resolution without relying on object identity.
        items: list[tuple[tuple[str, str], ...]] = []
        for sub in subscriptions:
            items.append(
                (
                    ("channel_id", str(sub.get("channel_id", "")).strip()),
                    ("channel_name", str(sub.get("channel_name", "")).strip()),
                    ("name", str(sub.get("name", "")).strip()),
                    (
                        "startup_backfill_minutes",
                        str(sub.get("startup_backfill_minutes", "")).strip(),
                    ),
                    (
                        "backfill_interval_seconds",
                        str(sub.get("backfill_interval_seconds", "")).strip(),
                    ),
                    (
                        "backfill_overlap_seconds",
                        str(sub.get("backfill_overlap_seconds", "")).strip(),
                    ),
                    ("backfill_limit", str(sub.get("backfill_limit", "")).strip()),
                    ("participation", _chat_participation(sub.get("participation"))),
                )
            )
        return tuple(items)

    def _backfill_policy_from_subscription(
        self, subscription: dict[str, Any]
    ) -> ChatBackfillPolicy:
        default = self._default_backfill_policy
        return ChatBackfillPolicy(
            startup_minutes=_positive_int(
                subscription.get("startup_backfill_minutes"), default.startup_minutes
            ),
            interval_seconds=_positive_float(
                subscription.get("backfill_interval_seconds"),
                default.interval_seconds,
            ),
            overlap_seconds=_positive_float(
                subscription.get("backfill_overlap_seconds"),
                default.overlap_seconds,
            ),
            limit=max(
                1, _positive_int(subscription.get("backfill_limit"), default.limit)
            ),
            participation=_chat_participation(subscription.get("participation")),
        )

    async def _resolve_subscription_channels(
        self, person: Person, subscriptions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        person_context = self.context.clone_for(person)
        chat_service = person_context.get_chat_service()
        try:
            resolved: list[dict[str, Any]] = []
            for sub in subscriptions:
                item = dict(sub)
                channel_id = str(item.get("channel_id", "")).strip()
                if channel_id:
                    resolved.append(item)
                    continue
                channel_name = str(item.get("channel_name", "")).strip()
                if not channel_name:
                    continue
                resolved_id = await chat_service.resolve_channel_id(channel_name)
                if not resolved_id:
                    self._log_info(
                        "chat subscription skipped: person=%s channel_name=%s could not be resolved",
                        person.person_id,
                        channel_name,
                    )
                    continue
                item["channel_id"] = resolved_id
                resolved.append(item)
            return resolved
        finally:
            await person_context.aclose()

    def _get_or_create_listener(
        self, key: str, subscribers: list[tuple[Person, ResolvedSubscriptions]]
    ) -> EventListener:
        listener = self._listeners.get(key)
        if listener is not None:
            return listener
        listener = self._factory.create_event_listener(
            self.context.logger,
            self.context.team,
            [person for person, _ in subscribers],
            self._wake_receiver,
        )
        self._listeners[key] = listener
        self._log_info(
            "created chat listener: service=%s members=%s",
            self._service,
            ",".join(person.person_id for person, _ in subscribers),
        )
        return listener

    def _queue_event(self, person: Person, event: ChatEvent, participation: str) -> int:
        """Queue ``event`` for ``person`` unless it is done; 1 when queued."""
        args = (self._service, person.person_id, event.channel_id)
        if self._state_store.is_processed_event(*args, event.event_id):
            return 0
        if is_suppressed_chat_event(event):
            self._state_store.mark_processed_event(*args, event.event_id)
            return 0
        self._state_store.upsert_pending_event(*args, event, participation)
        return 1

    async def _backfill_due_events(
        self, person: Person, channel_id: str, policy: ChatBackfillPolicy
    ) -> int:
        key = (self._service, person.person_id, channel_id)
        now = time.monotonic()
        startup_due = key not in self._startup_backfilled
        periodic_due = (
            not startup_due
            and policy.interval_seconds > 0
            and now - self._last_backfill_at.get(key, 0.0) >= policy.interval_seconds
        )
        if not startup_due and not periodic_due:
            return 0
        try:
            return await self._backfill_channel_and_threads(person, channel_id, policy)
        except Exception as exc:
            self._log_warning(
                "chat backfill skipped: person=%s service=%s channel=%s error=%s",
                person.person_id,
                self._service,
                channel_id,
                exc,
            )
            return 0
        finally:
            self._startup_backfilled.add(key)
            self._last_backfill_at[key] = now

    async def _backfill_channel_and_threads(
        self, person: Person, channel_id: str, policy: ChatBackfillPolicy
    ) -> int:
        scope = (self._service, person.person_id, channel_id)
        person_context = self.context.clone_for(person)
        chat_service = person_context.get_chat_service()
        # A recorded receive reset is a hard floor: never fetch or queue events
        # at or before it, even when a watermark minus overlap reaches further.
        cutoff = self._state_store.load_receive_cutoff(*scope[:2])
        try:
            count = await self._backfill_channel_events(
                person, channel_id, chat_service, policy, cutoff
            )
            for thread_state in self._state_store.list_thread_states(*scope):
                if thread_state.backfill_disabled_reason:
                    continue
                try:
                    count += await self._backfill_thread_events(
                        person,
                        channel_id,
                        thread_state.thread_id,
                        chat_service,
                        policy,
                        cutoff,
                    )
                except ChatThreadNotFoundError:
                    await self._write_backfill(
                        scope,
                        partial(
                            self._disable_thread_backfill,
                            person,
                            channel_id,
                            thread_state,
                            "thread_not_found",
                        ),
                    )
            return count
        finally:
            await person_context.aclose()

    async def _backfill_channel_events(
        self,
        person: Person,
        channel_id: str,
        chat_service: ChatService,
        policy: ChatBackfillPolicy,
        cutoff: datetime | None,
    ) -> int:
        scope = (self._service, person.person_id, channel_id)
        state = self._state_store.load_channel_cursor(*scope)
        if state.watermark:
            since = state.watermark - timedelta(seconds=policy.overlap_seconds)
        elif policy.startup_minutes > 0:
            since = datetime.now(UTC) - timedelta(minutes=policy.startup_minutes)
        else:
            return 0
        if cutoff:
            since = max(since, cutoff)
        cursor: str | None = None
        watermark = state.watermark
        count = 0
        while not self._stop_event.is_set():
            page = await chat_service.list_channel_events(
                channel_id, cursor=cursor, since=since, limit=policy.limit
            )
            for event in page.events:
                watermark = max(filter(None, (watermark, event.occurred_at)))
                if cutoff and event.occurred_at <= cutoff:
                    continue
                count += await self._write_backfill(
                    scope,
                    partial(self._queue_event, person, event, policy.participation),
                )
            cursor = page.cursor
            if not cursor:
                break
        if watermark != state.watermark:
            state.watermark = watermark
            await self._write_backfill(
                scope, partial(self._state_store.save_channel_cursor, *scope, state)
            )
        return count

    def _disable_thread_backfill(
        self,
        person: Person,
        channel_id: str,
        thread_state: ThreadConversationState,
        reason: str,
    ) -> None:
        thread_state.backfill_disabled_reason = reason
        thread_state.backfill_error_count += 1
        thread_state.last_backfill_error = reason
        self._state_store.save_thread_state(
            self._service,
            person.person_id,
            channel_id,
            thread_state.thread_id,
            thread_state,
        )
        self._log_info(
            "chat thread backfill disabled: person=%s service=%s channel=%s thread=%s reason=%s",
            person.person_id,
            self._service,
            channel_id,
            thread_state.thread_id,
            reason,
        )

    async def _backfill_thread_events(
        self,
        person: Person,
        channel_id: str,
        thread_id: str,
        chat_service: ChatService,
        policy: ChatBackfillPolicy,
        cutoff: datetime | None,
    ) -> int:
        scope = (self._service, person.person_id, channel_id)
        cached = self._state_store.load_thread_messages(*scope, thread_id)
        # Only what arrived since the newest message known, less the overlap;
        # all of a thread nothing is known of yet.
        newest = max((message.occurred_at for message in cached), default=None)
        since = newest - timedelta(seconds=policy.overlap_seconds) if newest else None
        cursor: str | None = None
        count = 0
        while not self._stop_event.is_set():
            page = await chat_service.list_thread_events(
                channel_id, thread_id=thread_id, cursor=cursor, limit=policy.limit
            )
            for event in page.events:
                if (since and event.occurred_at < since) or (
                    cutoff and event.occurred_at <= cutoff
                ):
                    continue
                count += await self._write_backfill(
                    scope,
                    partial(self._queue_event, person, event, policy.participation),
                )
            cursor = page.cursor
            if not cursor:
                break
        return count

    async def _aclose_sources(self) -> None:
        for subscribers in self._connection_subscriptions.values():
            for person, channels in subscribers:
                for channel_id in channels:
                    try:
                        self._receive_status.save(
                            self._service,
                            person.person_id,
                            channel_id,
                            state="unavailable",
                        )
                    except OSError as exc:
                        self._log_warning(
                            "chat receiver shutdown status failed: %s", exc
                        )
        self._connection_subscriptions.clear()
        for listener in list(self._listeners.values()):
            try:
                listener.stop()
            except Exception:
                continue
        self._listeners = {}
        self._subscription_channel_cache = {}
        self._last_group_log_state = None
        self._last_backfill_at = {}
        self._startup_backfilled = set()

    def _log_info(self, msg: str, *args: Any) -> None:
        logger = getattr(self.context, "logger", None)
        if logger is None:
            return
        try:
            logger.info(msg, *args)
        except Exception:
            return

    def _log_warning(self, msg: str, *args: Any) -> None:
        logger = getattr(self.context, "logger", None)
        if logger is None:
            return
        try:
            logger.warning(msg, *args)
        except Exception:
            return


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _chat_participation(value: Any) -> str:
    participation = str(value or "strict").strip().lower()
    if participation in {"strict", "social", "muted"}:
        return participation
    return "strict"
