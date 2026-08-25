"""Common publish/watch runtime shared by Desktop and ``guildbotics start``."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from guildbotics.hub.relay import HEAD_UPDATED_EVENT, LIVE_EXPIRED_EVENT_KIND
from guildbotics.hub.relay_client import HubRelayClient, HubRelayClientError
from guildbotics.runtime.live_state import LiveState, LiveStatePublisher
from guildbotics.utils.workspace_sync_port import SHARED_RECORD_SCHEMA_VERSION

LIVE_HEARTBEAT_INTERVAL_SECONDS = 10.0
OWNER_CACHE_SECONDS = 5.0


class RelayRuntime:
    """Own one publisher and one reconnecting watcher for a process."""

    def __init__(
        self,
        client: HubRelayClient,
        *,
        on_live_state: Callable[[LiveState], None] | None = None,
        on_live_expired: Callable[[str, str, str], None] | None = None,
        on_head_updated: Callable[[], None] | None = None,
        on_relay_error: Callable[[str], None] | None = None,
        heartbeat_interval: float = LIVE_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        self._on_live_state = on_live_state
        self._on_live_expired = on_live_expired
        self._on_head_updated = on_head_updated
        self._on_relay_error = on_relay_error
        self._last_relay_error: str | None = None
        self._owner_cache: tuple[float, bool] | None = None
        self._owner_cache_lock = threading.Lock()
        self._heartbeat_interval = heartbeat_interval
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self.publisher = LiveStatePublisher(
            client.workspace_id,
            client.device_id,
            client.publish_line,
            publisher_id=client.publisher_id,
        )

    def start(self) -> None:
        if any(
            thread is not None and thread.is_alive()
            for thread in (self._watcher, self._heartbeat_thread)
        ):
            return
        self._stop.clear()
        self.publisher.heartbeat()
        self._watcher = threading.Thread(
            target=self._watch,
            name="guildbotics-hub-live-watch",
            daemon=True,
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            name="guildbotics-hub-live-heartbeat",
            daemon=True,
        )
        self._watcher.start()
        self._heartbeat_thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        self.client.close()
        with self._owner_cache_lock:
            self._owner_cache = None
        threads = [self._watcher, self._heartbeat_thread]
        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread is not None and thread.is_alive() for thread in threads):
            return False
        self._watcher = None
        self._heartbeat_thread = None
        return True

    def check_owner(self) -> bool | None:
        """Return owner status, or ``None`` when the Hub cannot answer."""
        now = time.monotonic()
        with self._owner_cache_lock:
            cached = self._owner_cache
            if cached is not None and now - cached[0] < OWNER_CACHE_SECONDS:
                return cached[1]
        try:
            owner = self.client.owner_get()
        except (HubRelayClientError, OSError, RuntimeError):
            return None
        result = owner is not None and owner.owner_device_id == self.client.device_id
        with self._owner_cache_lock:
            self._owner_cache = (time.monotonic(), result)
        return result

    def _watch(self) -> None:
        self.client.watch(self._handle_line, self._stop)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            self.publisher.heartbeat()

    def _handle_line(self, line: str) -> None:
        if line == HEAD_UPDATED_EVENT:
            if self._on_head_updated is not None:
                self._on_head_updated()
            return
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._report_relay_error("live_invalid")
            return
        if isinstance(payload, dict) and payload.get("kind") == LIVE_EXPIRED_EVENT_KIND:
            if _requires_client_update(payload):
                self._report_relay_error("live_client_update_required")
                return
            if (
                payload.get("workspace_id") == self.client.workspace_id
                and isinstance(payload.get("device_id"), str)
                and isinstance(payload.get("publisher_id"), str)
                and isinstance(payload.get("observed_at"), str)
                and self._on_live_expired is not None
            ):
                self._clear_relay_error()
                self._on_live_expired(
                    payload["device_id"],
                    payload["publisher_id"],
                    payload["observed_at"],
                )
            return
        if _requires_client_update(payload):
            self._report_relay_error("live_client_update_required")
            return
        try:
            state = LiveState.model_validate(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            if _requires_client_update(payload):
                self._report_relay_error("live_client_update_required")
            else:
                self._report_relay_error("live_invalid")
            return
        if state.workspace_id != self.client.workspace_id:
            return
        self._clear_relay_error()
        if self._on_live_state is not None:
            self._on_live_state(state)

    def _report_relay_error(self, code: str) -> None:
        if self._last_relay_error == code:
            return
        self._last_relay_error = code
        if self._on_relay_error is not None:
            self._on_relay_error(code)

    def _clear_relay_error(self) -> None:
        self._last_relay_error = None


def _requires_client_update(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("schema_version"), int)
        and payload["schema_version"] > SHARED_RECORD_SCHEMA_VERSION
    )
