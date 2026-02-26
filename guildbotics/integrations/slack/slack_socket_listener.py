from __future__ import annotations

import json
import queue
import re
import threading
import time
from logging import Logger
from typing import Any, Callable

import httpx

from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.runtime.event_listener import EventListener, IncomingChatEvent

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


class SlackSocketEventListener(EventListener):
    """Slack Socket Mode listener that only manages receive/ack/queue lifecycle."""

    def __init__(
        self,
        *,
        logger: Logger,
        app_token: str,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        ws_connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._logger = logger
        self._app_token = app_token
        self._base_url = (base_url or "https://slack.com/api").rstrip("/")
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._ws_connect = ws_connect

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._ws: Any | None = None
        self._ws_lock = threading.Lock()
        self._queue: queue.Queue[IncomingChatEvent] = queue.Queue()
        self._connect_attempts = 0
        self._reconnects = 0
        self._received_events = 0
        self._acks_sent = 0

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._reader_thread_main,
                name="gb-slack-socket-listener",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._get_ws()
        if ws is not None:
            _close_sync(ws)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._clear_ws()
        if self._owns_http_client and self._http_client is not None:
            self._http_client.close()
            self._http_client = None
        self._log_info(
            "slack socket listener stopped: base_url=%s connect_attempts=%d reconnects=%d received_events=%d acks_sent=%d",
            self._base_url,
            self._connect_attempts,
            self._reconnects,
            self._received_events,
            self._acks_sent,
        )

    def drain_events(self) -> list[IncomingChatEvent]:
        out: list[IncomingChatEvent] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        out.sort(key=lambda item: item.event.message_ts)
        return out

    def _reader_thread_main(self) -> None:
        while not self._stop_event.is_set():
            ws = None
            try:
                self._connect_attempts += 1
                if self._connect_attempts > 1:
                    self._reconnects += 1
                    self._log_info(
                        "slack socket listener reconnecting: base_url=%s reconnects=%d",
                        self._base_url,
                        self._reconnects,
                    )
                ws_url = self._open_socket_url()
                ws = self._connect_ws(ws_url)
                self._set_ws(ws)
                self._log_debug("socket listener connected")
                while not self._stop_event.is_set():
                    raw = ws.recv()
                    payload = self._parse_json(raw)
                    if not payload:
                        continue
                    item = self._to_incoming_event(payload)
                    if item is not None:
                        self._received_events += 1
                        self._queue.put(item)
                    self._ack_if_needed(ws, payload)
            except BaseException as e:
                if not self._stop_event.is_set():
                    self._log_debug(f"socket listener loop stopped ({type(e).__name__}: {e})")
            finally:
                self._clear_ws_if_same(ws)
                if ws is not None:
                    _close_sync(ws)
            if not self._stop_event.is_set():
                time.sleep(1.0)

    def _open_socket_url(self) -> str:
        client = self._get_http_client()
        response = client.post(f"{self._base_url}/apps.connections.open")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok", False):
            error = (
                payload.get("error", "unknown_error")
                if isinstance(payload, dict)
                else "invalid_json"
            )
            raise RuntimeError(f"Slack API 'apps.connections.open' failed: {error}")
        url = str(payload.get("url", "") or "")
        if not url:
            raise RuntimeError("Slack API 'apps.connections.open' returned empty url.")
        return url

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=10.0,
                headers={"Authorization": f"Bearer {self._app_token}"},
            )
            self._owns_http_client = True
        return self._http_client

    def _connect_ws(self, url: str) -> Any:
        if self._ws_connect is not None:
            return self._ws_connect(url)
        try:
            from websockets.sync.client import connect  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Socket Mode requires the 'websockets' package. Install it before using "
                "message_channels[].chat.event_source=socket_mode."
            ) from e
        return connect(url)

    def _ack_if_needed(self, ws: Any, payload: dict[str, Any]) -> None:
        envelope_id = payload.get("envelope_id")
        if not envelope_id:
            return
        try:
            ws.send(json.dumps({"envelope_id": str(envelope_id)}))
            self._acks_sent += 1
        except BaseException:
            self._log_debug("socket listener ack failed")

    def _to_incoming_event(self, envelope: dict[str, Any]) -> IncomingChatEvent | None:
        if str(envelope.get("type", "")) != "events_api":
            return None
        body = envelope.get("payload")
        if not isinstance(body, dict):
            return None
        event = body.get("event")
        if not isinstance(event, dict):
            return None
        if str(event.get("type", "")) != "message":
            return None

        channel_id = str(event.get("channel", "") or "")
        if not channel_id:
            return None
        ts = str(event.get("ts", "") or "")
        if not ts:
            return None
        thread_ts = _str_or_none(event.get("thread_ts")) or ts
        subtype = str(event.get("subtype", ""))
        text = str(event.get("text", "") or "")
        author_id = _str_or_none(event.get("user"))
        chat_event = ChatEvent(
            event_id=f"{channel_id}:{ts}",
            channel_id=channel_id,
            message_ts=ts,
            thread_ts=thread_ts,
            author_id=author_id,
            text=text,
            mentions=_extract_mentions(text),
            is_message=True,
            is_edit_or_delete=subtype in {"message_changed", "message_deleted"},
            is_bot_message=bool(event.get("bot_id")) or subtype == "bot_message",
            is_thread_reply=(thread_ts != ts),
        )
        return IncomingChatEvent(service_name="slack", channel_id=channel_id, event=chat_event)

    def _parse_json(self, raw: Any) -> dict[str, Any] | None:
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            payload = json.loads(text)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _set_ws(self, ws: Any) -> None:
        with self._ws_lock:
            self._ws = ws

    def _get_ws(self) -> Any | None:
        with self._ws_lock:
            return self._ws

    def _clear_ws(self) -> None:
        with self._ws_lock:
            self._ws = None

    def _clear_ws_if_same(self, ws: Any) -> None:
        with self._ws_lock:
            if self._ws is ws:
                self._ws = None

    def _log_debug(self, message: str) -> None:
        try:
            self._logger.debug(message)
        except Exception:
            pass

    def _log_info(self, message: str, *args: Any) -> None:
        try:
            self._logger.info(message, *args)
        except Exception:
            pass


def _extract_mentions(text: str) -> list[str]:
    return _MENTION_RE.findall(text or "")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _close_sync(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return
