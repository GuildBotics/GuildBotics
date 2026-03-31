from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from typing import Any

from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_profile import (
    get_chat_slack_base_url,
    get_chat_subscriptions,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_socket_listener import (
    SlackSocketEventListener,
)
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    EventListener,
    IncomingChatEvent,
)

SubscriptionSignature = tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True, slots=True)
class SlackConnectionKey:
    service: str
    event_source: str
    app_token_hash: str
    base_url: str


class EventListenerRunner:
    """Run event-driven workflows in a dedicated worker thread.

    This is a skeleton runner used to decouple event-driven execution from TaskScheduler.
    Listener registration and shared connection management are added in follow-up steps.
    """

    def __init__(
        self,
        context: Context,
        workflow_command: str = "workflows/chat_conversation_workflow",
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self.context = context
        self.workflow_command = workflow_command
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._listeners: dict[SlackConnectionKey, EventListener] = {}
        self._listener_tokens: dict[SlackConnectionKey, str] = {}
        self._subscription_channel_cache: dict[str, tuple[SubscriptionSignature, set[str]]] = {}
        self._last_group_log_state: tuple[int, int] | None = None
        self._cycle_count = 0
        self._cycle_failure_count = 0
        self._events_drained_count = 0
        self._events_delivered_count = 0
        self._events_skipped_processed_count = 0
        self._state_store = FileConversationStateStore()

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

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    async def dispatch_incoming_event(self, person: Person, item: IncomingChatEvent) -> str:
        """Dispatch a single incoming event to the workflow for one person."""
        context = self.context.clone_for(person)
        context.shared_state[INCOMING_CHAT_EVENT_KEY] = item.to_shared_state()
        runner = CommandRunner(context, self.workflow_command, [])
        try:
            return await runner.run()
        finally:
            await context.aclose()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop())
        finally:
            try:
                loop.run_until_complete(self.context.aclose())
            finally:
                loop.close()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._cycle_count += 1
            try:
                await self._run_once()
            except Exception as exc:  # pragma: no cover - defensive worker guard
                self._cycle_failure_count += 1
                self._log_warning("event runner cycle failed: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)
        self._log_info(
            "event listener runner summary: cycles=%d cycle_failures=%d drained=%d delivered=%d skipped_processed=%d",
            self._cycle_count,
            self._cycle_failure_count,
            self._events_drained_count,
            self._events_delivered_count,
            self._events_skipped_processed_count,
        )
        await self._aclose_sources()

    async def _run_once(self) -> None:
        await self._drain_and_dispatch_grouped()

    async def _drain_and_dispatch_grouped(self) -> None:
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
            delivered_count = 0
            skipped_processed_count = 0
            for incoming in drained_events:
                if self._stop_event.is_set():
                    break
                for person, channel_ids in person_subs:
                    if incoming.channel_id not in channel_ids:
                        continue
                    if self._is_processed_for_person(person, incoming):
                        skipped_processed_count += 1
                        continue
                    await self.dispatch_incoming_event(person, incoming)
                    self._mark_processed_for_person(person, incoming)
                    delivered_count += 1
            self._events_drained_count += len(drained_events)
            self._events_delivered_count += delivered_count
            self._events_skipped_processed_count += skipped_processed_count
            if drained_events:
                self._log_info(
                    "listener dispatch summary: base_url=%s token_hash=%s drained=%d delivered=%d skipped_processed=%d subscribers=%d total_drained=%d total_delivered=%d total_skipped_processed=%d",
                    key.base_url,
                    key.app_token_hash[:12],
                    len(drained_events),
                    delivered_count,
                    skipped_processed_count,
                    len(person_subs),
                    self._events_drained_count,
                    self._events_delivered_count,
                    self._events_skipped_processed_count,
                )

    async def _build_person_subscriptions_by_connection(
        self,
    ) -> dict[SlackConnectionKey, list[tuple[Person, set[str]]]]:
        grouped: dict[SlackConnectionKey, list[tuple[Person, set[str]]]] = {}
        for person in self.context.team.members:
            if self._stop_event.is_set():
                break
            if not getattr(person, "is_active", False):
                continue
            subscriptions = self._socket_subscriptions_for_person(person)
            if not subscriptions:
                continue
            channel_ids = await self._resolve_subscription_channel_ids_cached(
                person, subscriptions
            )
            if not channel_ids:
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
            grouped.setdefault(key, []).append((person, channel_ids))
        return grouped

    async def _resolve_subscription_channel_ids_cached(
        self,
        person: Person,
        subscriptions: list[dict[str, Any]],
    ) -> set[str]:
        signature = self._subscription_signature(subscriptions)
        cached = self._subscription_channel_cache.get(person.person_id)
        if cached is not None and cached[0] == signature:
            return set(cached[1])

        resolved_subs = await self._resolve_subscription_channels(person, subscriptions)
        if not resolved_subs:
            self._subscription_channel_cache.pop(person.person_id, None)
            return set()

        channel_ids = {
            str(sub.get("channel_id", "")).strip()
            for sub in resolved_subs
            if str(sub.get("channel_id", "")).strip()
        }
        if not channel_ids:
            self._subscription_channel_cache.pop(person.person_id, None)
            return set()

        self._subscription_channel_cache[person.person_id] = (signature, set(channel_ids))
        return channel_ids

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
                )
            )
        return tuple(items)

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
        base_url = (get_chat_slack_base_url(person) or "https://slack.com/api").rstrip("/")
        return (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash=hashlib.sha256(app_token.encode("utf-8")).hexdigest(),
                base_url=base_url,
            ),
            app_token,
        )

    def _is_processed_for_person(self, person: Person, incoming: IncomingChatEvent) -> bool:
        return self._state_store.is_processed_event(
            incoming.service_name,
            person.person_id,
            incoming.channel_id,
            incoming.event.event_id,
        )

    def _mark_processed_for_person(self, person: Person, incoming: IncomingChatEvent) -> None:
        self._state_store.mark_processed_event(
            incoming.service_name,
            person.person_id,
            incoming.channel_id,
            incoming.event.event_id,
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
