from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from guildbotics.entities.team import Person
from guildbotics.integrations.chat_profile import (
    get_chat_slack_base_url,
    get_chat_subscriptions,
)
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ConversationStateStore,
    ThreadConversationState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_chat_service import SlackApiError
from guildbotics.integrations.slack.slack_socket_listener import (
    SlackSocketEventListener,
)
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import (
    EventListener,
    IncomingChatEvent,
)

SubscriptionSignature = tuple[tuple[tuple[str, str], ...], ...]
ResolvedSubscriptions = dict[str, "ChatBackfillPolicy"]


@dataclass(frozen=True, slots=True)
class ChatBackfillPolicy:
    startup_minutes: int = 60
    interval_seconds: float = 300.0
    overlap_seconds: float = 60.0
    limit: int = 100
    participation: str = "strict"


@dataclass(frozen=True, slots=True)
class SlackConnectionKey:
    service: str
    event_source: str
    app_token_hash: str
    base_url: str


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
    ) -> None:
        self.context = context
        self.service_run_id = service_run_id
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._default_backfill_policy = ChatBackfillPolicy(
            startup_minutes=max(0, int(startup_backfill_minutes)),
            interval_seconds=max(0.0, float(backfill_interval_seconds)),
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        # Set inside the worker thread so stop() can cancel an in-flight cycle.
        # The cycle only drains/backfills, but backfill awaits Slack HTTP requests
        # that the stop event cannot interrupt; cancelling the cycle aborts those
        # awaits so a stop overlapping a backfill does not exceed the stop timeout.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_cycle: asyncio.Task[None] | None = None
        self._listeners: dict[SlackConnectionKey, EventListener] = {}
        self._listener_tokens: dict[SlackConnectionKey, str] = {}
        self._connection_person_ids: dict[SlackConnectionKey, list[str]] = {}
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
        # awaited Slack request instead of waiting it out past the stop timeout.
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
            if getattr(listener, "auth_failed", False):
                auth_failed_count += 1
                auth_failed_persons.extend(self._connection_person_ids.get(key, []))
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

    async def _run_loop(self) -> None:
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
        await self._aclose_sources()

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
        for key, person_subs in grouped.items():
            if self._stop_event.is_set():
                break
            listener = self._get_or_create_listener(key)
            listener.start()
            drained_events = listener.drain_events()
            pending_count = 0
            for incoming in drained_events:
                if self._stop_event.is_set():
                    break
                for person, subscriptions in person_subs:
                    if incoming.channel_id not in subscriptions:
                        continue
                    if self._is_processed_for_person(person, incoming):
                        continue
                    self._state_store.upsert_pending_event(
                        incoming.service_name,
                        person.person_id,
                        incoming.channel_id,
                        incoming.event,
                        subscriptions[incoming.channel_id].participation,
                    )
                    pending_count += 1
            # Backfill members concurrently (Slack I/O only). The actual chat
            # workflow runs later in each member's scheduler worker, which keeps
            # a member's chat/ticket/scheduled work on one serial queue.
            backfilled_results = await asyncio.gather(
                *(
                    self._backfill_person(person, key.service, subscriptions)
                    for person, subscriptions in person_subs
                )
            )
            backfilled_count = sum(backfilled_results)
            self._events_drained_count += len(drained_events)
            self._events_pending_count += pending_count
            self._events_backfilled_count += backfilled_count
            if drained_events or backfilled_count:
                self._log_info(
                    "listener queue summary: base_url=%s token_hash=%s drained=%d "
                    "pending=%d backfilled=%d subscribers=%d total_drained=%d "
                    "total_pending=%d total_backfilled=%d",
                    key.base_url,
                    key.app_token_hash[:12],
                    len(drained_events),
                    pending_count,
                    backfilled_count,
                    len(person_subs),
                    self._events_drained_count,
                    self._events_pending_count,
                    self._events_backfilled_count,
                )

    async def _backfill_person(
        self,
        person: Person,
        service: str,
        subscriptions: ResolvedSubscriptions,
    ) -> int:
        """Backfill one member's channels into the pending queue (no execution)."""
        backfilled = 0
        for channel_id, policy in subscriptions.items():
            if self._stop_event.is_set():
                break
            backfilled += await self._backfill_due_events(
                person, service, channel_id, policy
            )
        return backfilled

    async def _build_person_subscriptions_by_connection(
        self,
    ) -> dict[SlackConnectionKey, list[tuple[Person, ResolvedSubscriptions]]]:
        grouped: dict[
            SlackConnectionKey, list[tuple[Person, ResolvedSubscriptions]]
        ] = {}
        for person in self.context.team.members:
            if self._stop_event.is_set():
                break
            if not getattr(person, "is_active", False):
                continue
            subscriptions = self._socket_subscriptions_for_person(person)
            if not subscriptions:
                continue
            resolved = await self._resolve_subscriptions_cached(person, subscriptions)
            if not resolved:
                continue
            try:
                key, app_token = self._make_connection_key(person)
            except ValueError as e:
                self._log_warning(
                    "event listener runner skipped person=%s due to invalid socket_mode config: %s",
                    person.person_id,
                    e,
                )
                continue
            self._listener_tokens.setdefault(key, app_token)
            grouped.setdefault(key, []).append((person, resolved))
        self._connection_person_ids = {
            key: [person.person_id for person, _ in subs]
            for key, subs in grouped.items()
        }
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
                    ("service", str(sub.get("service", "slack")).strip().lower()),
                    (
                        "event_source",
                        str(sub.get("event_source", "socket_mode")).strip().lower(),
                    ),
                    ("enabled", "1" if bool(sub.get("enabled", True)) else "0"),
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

    def _socket_subscriptions_for_person(self, person: Person) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sub in get_chat_subscriptions(person):
            if not isinstance(sub, dict):
                continue
            if not bool(sub.get("enabled", True)):
                continue
            if str(sub.get("service", "slack")).strip().lower() != "slack":
                continue
            source_kind = str(sub.get("event_source", "socket_mode")).strip().lower()
            if source_kind != "socket_mode":
                continue
            out.append(dict(sub))
        return out

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

    def _get_or_create_listener(self, key: SlackConnectionKey) -> EventListener:
        listener = self._listeners.get(key)
        if listener is not None:
            return listener
        app_token = self._listener_tokens.get(key)
        if not app_token:
            raise RuntimeError("Missing app token for listener creation.")
        listener = SlackSocketEventListener(
            logger=self.context.logger,
            app_token=app_token,
            base_url=key.base_url,
            person_ids=self._connection_person_ids.get(key, []),
        )
        self._listeners[key] = listener
        self._log_info(
            "created slack listener: service=%s event_source=%s base_url=%s token_hash=%s",
            key.service,
            key.event_source,
            key.base_url,
            key.app_token_hash[:12],
        )
        return listener

    def _make_connection_key(self, person: Person) -> tuple[SlackConnectionKey, str]:
        if not person.has_secret("SLACK_APP_TOKEN"):
            env_key = person.to_person_env_key("SLACK_APP_TOKEN")
            raise ValueError(
                f"Slack App Token is required for person '{person.person_id}'. "
                f"Set environment variable '{env_key}'."
            )
        app_token = person.get_secret("SLACK_APP_TOKEN")
        base_url = (get_chat_slack_base_url(person) or "https://slack.com/api").rstrip(
            "/"
        )
        return (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash=hashlib.sha256(app_token.encode("utf-8")).hexdigest(),
                base_url=base_url,
            ),
            app_token,
        )

    def _is_processed_for_person(
        self, person: Person, incoming: IncomingChatEvent
    ) -> bool:
        return self._state_store.is_processed_event(
            incoming.service_name,
            person.person_id,
            incoming.channel_id,
            incoming.event.event_id,
        )

    async def _backfill_due_events(
        self,
        person: Person,
        service_name: str,
        channel_id: str,
        policy: ChatBackfillPolicy,
    ) -> int:
        key = (service_name, person.person_id, channel_id)
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
            return await self._backfill_channel_and_threads(
                person, service_name, channel_id, policy
            )
        except Exception as exc:
            self._log_warning(
                "chat backfill skipped: person=%s service=%s channel=%s error=%s",
                person.person_id,
                service_name,
                channel_id,
                exc,
            )
            return 0
        finally:
            self._startup_backfilled.add(key)
            self._last_backfill_at[key] = now

    async def _backfill_channel_and_threads(
        self,
        person: Person,
        service_name: str,
        channel_id: str,
        policy: ChatBackfillPolicy,
    ) -> int:
        person_context = self.context.clone_for(person)
        chat_service = person_context.get_chat_service()
        try:
            count = await self._backfill_channel_events(
                person, service_name, channel_id, chat_service, policy
            )
            for thread_state in self._state_store.list_thread_states(
                service_name, person.person_id, channel_id
            ):
                if thread_state.backfill_disabled_reason:
                    continue
                try:
                    count += await self._backfill_thread_events(
                        person,
                        service_name,
                        channel_id,
                        thread_state.thread_ts,
                        chat_service,
                        policy,
                    )
                except SlackApiError as exc:
                    if exc.method != "conversations.replies" or (
                        exc.error != "thread_not_found"
                    ):
                        raise
                    self._disable_thread_backfill(
                        person,
                        service_name,
                        channel_id,
                        thread_state,
                        exc.error,
                    )
            return count
        finally:
            await person_context.aclose()

    async def _backfill_channel_events(
        self,
        person: Person,
        service_name: str,
        channel_id: str,
        chat_service: Any,
        policy: ChatBackfillPolicy,
    ) -> int:
        state = self._state_store.load_channel_cursor(
            service_name, person.person_id, channel_id
        )
        oldest_ts = self._backfill_oldest_ts(state.oldest_ts, policy)
        if oldest_ts is None:
            return 0
        cursor: str | None = None
        highest_ts = state.oldest_ts
        count = 0
        while not self._stop_event.is_set():
            page = await chat_service.list_channel_events(
                channel_id,
                cursor=cursor,
                oldest_ts=oldest_ts,
                limit=policy.limit,
            )
            for event in page.events:
                count += self._upsert_backfilled_event(
                    service_name, person, channel_id, event, policy.participation
                )
            highest_ts = _max_slack_ts(highest_ts, page.oldest_ts)
            cursor = page.cursor
            if not cursor:
                break
        self._save_backfill_watermark(
            service_name, person.person_id, channel_id, state, highest_ts
        )
        return count

    def _disable_thread_backfill(
        self,
        person: Person,
        service_name: str,
        channel_id: str,
        thread_state: ThreadConversationState,
        reason: str,
    ) -> None:
        thread_state.backfill_disabled_reason = reason
        thread_state.backfill_error_count += 1
        thread_state.last_backfill_error = reason
        self._state_store.save_thread_state(
            service_name,
            person.person_id,
            channel_id,
            thread_state.thread_ts,
            thread_state,
        )
        self._log_info(
            "chat thread backfill disabled: person=%s service=%s channel=%s thread=%s reason=%s",
            person.person_id,
            service_name,
            channel_id,
            thread_state.thread_ts,
            reason,
        )

    async def _backfill_thread_events(
        self,
        person: Person,
        service_name: str,
        channel_id: str,
        thread_ts: str,
        chat_service: Any,
        policy: ChatBackfillPolicy,
    ) -> int:
        thread_messages = self._state_store.load_thread_messages(
            service_name, person.person_id, channel_id, thread_ts
        )
        latest_message_ts = max(
            (message.message_ts for message in thread_messages), default=thread_ts
        )
        oldest_ts = _slack_ts_minus_seconds(latest_message_ts, policy.overlap_seconds)
        cursor: str | None = None
        count = 0
        while not self._stop_event.is_set():
            page = await chat_service.list_thread_events(
                channel_id,
                thread_ts=thread_ts,
                cursor=cursor,
                limit=policy.limit,
            )
            for event in page.events:
                if _compare_slack_ts(event.message_ts, oldest_ts) < 0:
                    continue
                count += self._upsert_backfilled_event(
                    service_name, person, channel_id, event, policy.participation
                )
            cursor = page.cursor
            if not cursor:
                break
        return count

    def _upsert_backfilled_event(
        self,
        service_name: str,
        person: Person,
        channel_id: str,
        event: ChatEvent,
        participation: str,
    ) -> int:
        incoming = IncomingChatEvent(
            service_name=service_name, channel_id=channel_id, event=event
        )
        if self._is_processed_for_person(person, incoming):
            return 0
        self._state_store.upsert_pending_event(
            service_name, person.person_id, channel_id, event, participation
        )
        return 1

    def _backfill_oldest_ts(
        self, watermark_ts: str | None, policy: ChatBackfillPolicy
    ) -> str | None:
        if watermark_ts:
            return _slack_ts_minus_seconds(watermark_ts, policy.overlap_seconds)
        if policy.startup_minutes <= 0:
            return None
        return _format_slack_ts(time.time() - policy.startup_minutes * 60)

    def _save_backfill_watermark(
        self,
        service_name: str,
        person_id: str,
        channel_id: str,
        state: ChannelCursorState,
        highest_ts: str | None,
    ) -> None:
        if not highest_ts:
            return
        self._state_store.save_channel_cursor(
            service_name,
            person_id,
            channel_id,
            ChannelCursorState(
                cursor=None,
                oldest_ts=highest_ts,
                processed_event_ids=state.processed_event_ids,
            ),
        )

    async def _aclose_sources(self) -> None:
        for listener in list(self._listeners.values()):
            try:
                listener.stop()
            except Exception:
                continue
        self._listeners = {}
        self._listener_tokens = {}
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


def _format_slack_ts(value: float) -> str:
    return f"{max(0.0, value):.6f}"


def _slack_ts_minus_seconds(value: str, seconds: float) -> str:
    try:
        return _format_slack_ts(float(value) - seconds)
    except ValueError:
        return value


def _max_slack_ts(left: str | None, right: str | None) -> str | None:
    if not left:
        return right
    if not right:
        return left
    return left if _compare_slack_ts(left, right) >= 0 else right


def _compare_slack_ts(left: str, right: str) -> int:
    try:
        left_value = float(left)
        right_value = float(right)
    except ValueError:
        return (left > right) - (left < right)
    return (left_value > right_value) - (left_value < right_value)
