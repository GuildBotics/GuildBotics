from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ConversationStateStore,
    PendingChatEvent,
    ScheduledPostState,
    ThreadConversationState,
    ThreadHandoffState,
    ThreadMessageState,
    ThreadSystemNoticeState,
)
from guildbotics.integrations.chat_workflow_status import (
    normalize_workflow_status_metadata,
)
from guildbotics.intelligences.effort import normalize_effort
from guildbotics.utils.fileio import get_workspace_local_path, get_workspace_state_path
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    delete_shared_path,
    dump_shared_json,
    update_shared_text,
    write_shared_json,
)


class FileConversationStateStore(ConversationStateStore):
    """JSON file-backed state store for chat workflows.

    ``state/chat_state`` is shared, so every change here is a change another
    device can be making at the same instant, and several of them read the file
    they are about to write. The synchronization queue checks a hub's content
    out over these same files, and a read taken before that checkout and
    written back after it reinstates what the queue just adopted, as an
    ordinary local edit that the next cycle commits and pushes -- so the other
    device's change disappears without being recorded as a conflict. For chat
    state that means answering a message twice, or not at all.

    Those methods therefore rewrite through :meth:`_update_json`, which reads
    and writes inside one span of the workspace's shared-write lock -- the same
    lock the queue holds across its checkout and commit. A method that simply
    stores state its caller composed needs none of this and writes directly,
    under the span the port takes for it.

    The in-process lock is for the thread message cache, which is device-local
    and so has no other writer for the workspace lock to exclude. It is taken
    inside the shared lock and never around it: the reverse order in even one
    method is what makes two locks deadlock.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        cache_dir: Path | None = None,
        max_processed_events: int = 500,
        max_thread_messages: int = 500,
    ):
        self._base_dir = (
            base_dir if base_dir is not None else get_workspace_state_path("chat_state")
        )
        self._cache_dir = (
            cache_dir
            if cache_dir is not None
            else get_workspace_local_path("chat-cache")
        )
        self._max_processed_events = max(1, int(max_processed_events))
        self._max_thread_messages = max(1, int(max_thread_messages))
        self._lock = threading.RLock()

    def load_channel_cursor(
        self, service: str, person_id: str, channel_id: str
    ) -> ChannelCursorState:
        with self._lock:
            path = self._channel_file(service, person_id, channel_id)
            return _channel_cursor_from(self._read_json(path))

    def save_channel_cursor(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        state: ChannelCursorState,
    ) -> None:
        self._write_json(
            self._channel_file(service, person_id, channel_id),
            self._cursor_payload(state),
        )

    def _cursor_payload(self, state: ChannelCursorState) -> dict:
        return {
            "cursor": state.cursor,
            "oldest_ts": state.oldest_ts,
            "processed_event_ids": _dedupe_keep_order(state.processed_event_ids)[
                -self._max_processed_events :
            ],
        }

    def is_processed_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> bool:
        with self._lock:
            state = self.load_channel_cursor(service, person_id, channel_id)
            return event_id in set(state.processed_event_ids)

    def mark_processed_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> None:
        def _mark(data: dict) -> dict:
            state = _channel_cursor_from(data)
            state.processed_event_ids.append(event_id)
            return self._cursor_payload(state)

        self._update_json(self._channel_file(service, person_id, channel_id), _mark)

    def load_thread_state(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> ThreadConversationState:
        data = self._read_thread_payload(service, person_id, channel_id, thread_ts)
        return _thread_state_from(data, channel_id=channel_id, thread_ts=thread_ts)

    def save_thread_state(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        thread_ts: str,
        state: ThreadConversationState,
    ) -> None:
        payload = asdict(state)
        payload["participants"] = sorted(state.participants)
        self._write_json(
            self._thread_file(service, person_id, channel_id, thread_ts), payload
        )

    def load_thread_messages(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> list[ThreadMessageState]:
        data = self._read_json(
            self._thread_cache_file(service, person_id, channel_id, thread_ts)
        )
        raw_items = data.get("messages") or []
        if not isinstance(raw_items, list):
            return []
        out: list[ThreadMessageState] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            message_ts = _to_str_or_none(item.get("message_ts"))
            if not message_ts:
                continue
            out.append(
                ThreadMessageState(
                    channel_id=str(item.get("channel_id", channel_id)),
                    thread_ts=str(item.get("thread_ts", thread_ts)),
                    message_ts=message_ts,
                    author_id=_to_str_or_none(item.get("author_id")),
                    text=str(item.get("text", "") or ""),
                    mentions=[str(x) for x in (item.get("mentions") or []) if str(x)],
                    is_bot_message=bool(item.get("is_bot_message", False)),
                )
            )
        return out

    def list_thread_states(
        self, service: str, person_id: str, channel_id: str
    ) -> list[ThreadConversationState]:
        thread_dir = self._thread_file(service, person_id, channel_id, "_").parent
        if not thread_dir.exists():
            return []
        out: list[ThreadConversationState] = []
        with self._lock:
            for path in sorted(thread_dir.glob("*.json")):
                thread_ts = path.stem
                state = self.load_thread_state(
                    service, person_id, channel_id, thread_ts
                )
                out.append(state)
        return out

    def append_thread_message(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        thread_ts: str,
        message: ThreadMessageState,
    ) -> None:
        item = {
            "channel_id": message.channel_id,
            "thread_ts": message.thread_ts,
            "message_ts": message.message_ts,
            "author_id": message.author_id,
            "text": message.text,
            "mentions": [str(x) for x in message.mentions if str(x)],
            "is_bot_message": bool(message.is_bot_message),
        }
        path = self._thread_cache_file(service, person_id, channel_id, thread_ts)
        # The message cache is device-local, so the in-process lock is the only
        # thing that can be writing it and its span ends here. Taking the shared
        # lock around it as well would put this method's two halves in the
        # opposite order to every other writer, which is how two locks deadlock.
        with self._lock:
            payload = self._read_json(path)
            raw_items = payload.get("messages") or []
            if not isinstance(raw_items, list):
                raw_items = []
            merged: list[dict] = []
            replaced = False
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                raw_ts = _to_str_or_none(raw.get("message_ts"))
                if raw_ts == message.message_ts:
                    merged.append(item)
                    replaced = True
                else:
                    merged.append(raw)
            if not replaced:
                merged.append(item)
            merged.sort(key=lambda x: str(x.get("message_ts", "")))
            self._write_json(path, {"messages": merged[-self._max_thread_messages :]})
        # The shared thread state is what makes the thread discoverable
        # (list_thread_states, backfill, handoff to another device). Only its
        # absence is filled in here, and "absent" has to be decided in the same
        # span that writes: answered a moment before the queue adopts a hub's
        # richer version of the same thread, this would replace it with a stub.
        self._update_json(
            self._thread_file(service, person_id, channel_id, thread_ts),
            lambda data: data or {"channel_id": channel_id, "thread_ts": thread_ts},
        )

    def load_scheduled_post_state(
        self, service: str, person_id: str, schedule_name: str
    ) -> ScheduledPostState:
        with self._lock:
            path = self._scheduled_post_file(service, person_id, schedule_name)
            data = self._read_json(path)
            if not data:
                return ScheduledPostState()
            return ScheduledPostState(
                last_run_slot=_to_str_or_none(data.get("last_run_slot"))
            )

    def save_scheduled_post_state(
        self,
        service: str,
        person_id: str,
        schedule_name: str,
        state: ScheduledPostState,
    ) -> None:
        self._write_json(
            self._scheduled_post_file(service, person_id, schedule_name),
            {"last_run_slot": state.last_run_slot},
        )

    def load_pending_events(
        self, service: str, person_id: str, channel_id: str
    ) -> list[PendingChatEvent]:
        with self._lock:
            data = self._read_json(
                self._pending_events_file(service, person_id, channel_id)
            )
            raw_items = data.get("events") or []
            if not isinstance(raw_items, list):
                return []
            out: list[PendingChatEvent] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                event_id = _to_str_or_none(item.get("event_id"))
                message_ts = _to_str_or_none(item.get("message_ts"))
                thread_ts = _to_str_or_none(item.get("thread_ts"))
                if not event_id or not message_ts or not thread_ts:
                    continue
                mentions = item.get("mentions") or []
                if not isinstance(mentions, list):
                    mentions = []
                out.append(
                    PendingChatEvent(
                        event=ChatEvent(
                            event_id=event_id,
                            channel_id=str(item.get("channel_id", channel_id)),
                            message_ts=message_ts,
                            thread_ts=thread_ts,
                            author_id=_to_str_or_none(item.get("author_id")),
                            text=str(item.get("text", "") or ""),
                            mentions=[str(x) for x in mentions if str(x)],
                            is_edit_or_delete=bool(
                                item.get("is_edit_or_delete", False)
                            ),
                            is_bot_message=bool(item.get("is_bot_message", False)),
                            is_thread_reply=bool(item.get("is_thread_reply", False)),
                            metadata=_pending_metadata(item.get("metadata")),
                        ),
                        chat_participation=str(
                            item.get("chat_participation", "strict") or "strict"
                        ),
                        attempt_count=_to_non_negative_int(item.get("attempt_count")),
                        max_attempts=max(
                            1, _to_non_negative_int(item.get("max_attempts")) or 5
                        ),
                        next_attempt_at=_to_str_or_none(item.get("next_attempt_at")),
                        run_id=str(item.get("run_id", "") or ""),
                        last_error_category=str(
                            item.get("last_error_category", "") or ""
                        ),
                        wake_cursor=str(item.get("wake_cursor", "") or ""),
                    )
                )
            return out

    def upsert_pending_event(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        event: ChatEvent,
        chat_participation: str = "strict",
    ) -> None:
        def _upsert(data: dict) -> dict:
            def _replacement(raw: dict) -> dict:
                # An event already queued keeps its retry bookkeeping: this is
                # the same message arriving again, not a new attempt at it.
                return _pending_event_to_item(
                    PendingChatEvent(
                        event=event,
                        chat_participation=chat_participation,
                        attempt_count=_to_non_negative_int(raw.get("attempt_count")),
                        max_attempts=max(
                            1, _to_non_negative_int(raw.get("max_attempts")) or 5
                        ),
                        next_attempt_at=_to_str_or_none(raw.get("next_attempt_at")),
                        run_id=str(raw.get("run_id", "") or ""),
                        last_error_category=str(
                            raw.get("last_error_category", "") or ""
                        ),
                        wake_cursor=str(raw.get("wake_cursor", "") or ""),
                    )
                )

            return self._merged_events(
                data,
                event.event_id,
                _replacement,
                lambda: _pending_event_to_item(
                    PendingChatEvent(event=event, chat_participation=chat_participation)
                ),
            )

        self._update_json(
            self._pending_events_file(service, person_id, channel_id), _upsert
        )

    def save_pending_event(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        pending: PendingChatEvent,
    ) -> None:
        item = _pending_event_to_item(pending)
        self._update_json(
            self._pending_events_file(service, person_id, channel_id),
            lambda data: self._merged_events(
                data, pending.event.event_id, lambda _raw: item, lambda: item
            ),
        )

    def _merged_events(
        self,
        data: dict,
        event_id: str,
        replacement: Callable[[dict], dict],
        addition: Callable[[], dict],
    ) -> dict:
        """Return ``data`` with the entry for ``event_id`` replaced or added."""
        raw_items = data.get("events") or []
        if not isinstance(raw_items, list):
            raw_items = []
        merged: list[dict] = []
        replaced = False
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            if _to_str_or_none(raw.get("event_id")) == event_id:
                merged.append(replacement(raw))
                replaced = True
            else:
                merged.append(raw)
        if not replaced:
            merged.append(addition())
        merged.sort(key=lambda x: str(x.get("message_ts", "")))
        return {**data, "events": merged[-self._max_processed_events :]}

    def remove_pending_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> None:
        def _drop(data: dict) -> dict | None:
            raw_items = data.get("events")
            if not isinstance(raw_items, list):
                # Nothing this method understands, including the absent file
                # that reads as an empty mapping: leave it as it is.
                return data or None
            filtered = [
                raw
                for raw in raw_items
                if isinstance(raw, dict)
                and _to_str_or_none(raw.get("event_id")) != event_id
            ]
            # An empty queue is stored as no file, so a channel that goes quiet
            # stops appearing in list_pending_channels. A removal that fails is
            # left to fail: the event stays queued and is attempted again, which
            # is the queue behaving as intended rather than damage to undo.
            return {**data, "events": filtered} if filtered else None

        self._update_json(
            self._pending_events_file(service, person_id, channel_id), _drop
        )

    def list_pending_channels(self, person_id: str) -> list[tuple[str, str]]:
        with self._lock:
            out: list[tuple[str, str]] = []
            if not self._base_dir.exists():
                return out
            safe_person = _safe_segment(person_id)
            for service_dir in sorted(self._base_dir.iterdir()):
                if not service_dir.is_dir():
                    continue
                pending_dir = service_dir / safe_person / "pending_events"
                if not pending_dir.is_dir():
                    continue
                for pending_file in sorted(pending_dir.glob("*.json")):
                    out.append((service_dir.name, pending_file.stem))
            return out

    def list_known_channels(self, service: str, person_id: str) -> list[str]:
        with self._lock:
            root = self._root(service, person_id)
            found: set[str] = set()
            for sub_dir in ("channels", "pending_events"):
                directory = root / sub_dir
                if directory.is_dir():
                    for path in directory.glob("*.json"):
                        found.add(path.stem)
            threads_dir = root / "threads"
            if threads_dir.is_dir():
                for path in threads_dir.iterdir():
                    if path.is_dir():
                        found.add(path.name)
            return sorted(found)

    def load_receive_cutoff(self, service: str, person_id: str) -> str | None:
        with self._lock:
            data = self._read_json(self._receive_cutoff_file(service, person_id))
            return _to_str_or_none(data.get("cutoff_ts"))

    def save_receive_cutoff(self, service: str, person_id: str, cutoff_ts: str) -> None:
        self._write_json(
            self._receive_cutoff_file(service, person_id),
            {"cutoff_ts": cutoff_ts},
        )

    def clear_channel_receive_backlog(
        self, service: str, person_id: str, channel_id: str
    ) -> None:
        # One span for the whole reset: the files removed here are chosen by
        # looking at what is on disk, and a queue checkout part-way through
        # would put back a backlog this reported as cleared.
        with shared_write_lock(), self._lock:
            # Drop received-but-unprocessed events for this channel. The pending
            # queue is drained without a cutoff check, so a stale file here would
            # be reprocessed -- and this is the one operation whose whole purpose
            # is that nothing stale is drained, so a failed unlink is answered
            # with an emptied file rather than left. (remove_pending_event has no
            # such fallback and needs none: a removal that fails there leaves an
            # event the queue simply attempts again, which is what it is for.)
            pending_file = self._pending_events_file(service, person_id, channel_id)
            if pending_file.exists():
                try:
                    self._remove(pending_file)
                except OSError:
                    self._write_json(pending_file, {"events": []})
            # Drop tracked threads as cleanup. Correctness does not depend on this
            # deletion: backfill of a surviving thread is bounded by the receive
            # cutoff filter in EventListenerRunner, so pre-cutoff replies are never
            # re-queued even if a file cannot be removed here.
            thread_dir = self._thread_file(service, person_id, channel_id, "_").parent
            cache_dir = self._thread_cache_file(
                service, person_id, channel_id, "_"
            ).parent
            for directory in (thread_dir, cache_dir):
                if not directory.is_dir():
                    continue
                for path in directory.glob("*.json"):
                    with suppress(Exception):
                        self._remove(path)
                with suppress(Exception):
                    directory.rmdir()

    def _root(self, service: str, person_id: str) -> Path:
        return self._base_dir / _safe_segment(service) / _safe_segment(person_id)

    def _receive_cutoff_file(self, service: str, person_id: str) -> Path:
        return self._root(service, person_id) / "receive_cutoff.json"

    def _channel_file(self, service: str, person_id: str, channel_id: str) -> Path:
        return (
            self._root(service, person_id)
            / "channels"
            / f"{_safe_segment(channel_id)}.json"
        )

    def _thread_file(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> Path:
        return (
            self._root(service, person_id)
            / "threads"
            / _safe_segment(channel_id)
            / f"{_safe_segment(thread_ts)}.json"
        )

    def _cache_root(self, service: str, person_id: str) -> Path:
        return self._cache_dir / _safe_segment(service) / _safe_segment(person_id)

    def _thread_cache_file(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> Path:
        return (
            self._cache_root(service, person_id)
            / "threads"
            / _safe_segment(channel_id)
            / f"{_safe_segment(thread_ts)}.json"
        )

    def _scheduled_post_file(
        self, service: str, person_id: str, schedule_name: str
    ) -> Path:
        return (
            self._root(service, person_id)
            / "scheduled_posts"
            / f"{_safe_segment(schedule_name)}.json"
        )

    def _pending_events_file(
        self, service: str, person_id: str, channel_id: str
    ) -> Path:
        return (
            self._root(service, person_id)
            / "pending_events"
            / f"{_safe_segment(channel_id)}.json"
        )

    def _read_thread_payload(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> dict:
        return self._read_json(
            self._thread_file(service, person_id, channel_id, thread_ts)
        )

    def _read_json(self, path: Path) -> dict:
        with self._lock:
            if not path.exists():
                return {}
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def _write_json(self, path: Path, payload: dict) -> None:
        # Conversation control state lives in the shared ``state/`` tree while the
        # thread message cache stays device-local; the shared write helpers
        # announce only the former, so both go through one write path.
        #
        # No in-process lock here. For a shared path the port takes the
        # workspace's lock, and taking this one outside it would be the reverse
        # of the order clear_channel_receive_backlog uses -- which is how two
        # locks deadlock. Callers that need the device-local cache held across
        # a read and a write take it themselves, around both.
        write_shared_json(path, self._stamped(payload))

    def _update_json(self, path: Path, apply: Callable[[dict], dict | None]) -> None:
        """Rewrite one file from what it holds, with the read inside the span.

        Damage is read as an empty mapping, the same way :meth:`_read_json`
        does it, so a file someone else corrupted is replaced rather than
        turning every later chat event into an error.
        """

        def _transform(current: Any | None) -> dict | None:
            payload = apply(current if isinstance(current, dict) else {})
            return None if payload is None else self._stamped(payload)

        def _tolerant(raw: str | None) -> str | None:
            payload = _transform(_loads(raw))
            return None if payload is None else dump_shared_json(payload)

        update_shared_text(path, _tolerant)

    @staticmethod
    def _stamped(payload: dict) -> dict:
        """Add the schema generation, last, so this build's value wins.

        Stamped in one place rather than in each of the five record kinds, so a
        new kind carries it without anyone remembering to add it -- and a kind
        that did not carry it would leave a build too old to read it no way to
        tell, since the reader of every field here defaults quietly.

        Last, because the rewriting methods hand back a mapping they read from
        the file: stamping first would write out whatever was already on disk,
        so after a move to 2 a record this build had just rewritten would still
        claim 1, and an older build would read it as one it understands. That
        is the exact failure the field exists to prevent.
        """
        return {**payload, "schema_version": SHARED_RECORD_SCHEMA_VERSION}

    def _remove(self, path: Path) -> None:
        delete_shared_path(path)


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _channel_cursor_from(data: dict) -> ChannelCursorState:
    if not data:
        return ChannelCursorState()
    processed = data.get("processed_event_ids") or []
    if not isinstance(processed, list):
        processed = []
    return ChannelCursorState(
        cursor=_to_str_or_none(data.get("cursor")),
        oldest_ts=_to_str_or_none(data.get("oldest_ts")),
        processed_event_ids=[str(item) for item in processed if str(item)],
    )


def _thread_state_from(
    data: dict, *, channel_id: str, thread_ts: str
) -> ThreadConversationState:
    if not data:
        return ThreadConversationState(channel_id=channel_id, thread_ts=thread_ts)
    participants = data.get("participants") or []
    if not isinstance(participants, list):
        participants = []
    handoffs = data.get("handoffs") or []
    if not isinstance(handoffs, list):
        handoffs = []
    system_notices = data.get("system_notices") or []
    if not isinstance(system_notices, list):
        system_notices = []
    return ThreadConversationState(
        channel_id=str(data.get("channel_id", channel_id)),
        thread_ts=str(data.get("thread_ts", thread_ts)),
        participants={str(item) for item in participants if str(item)},
        thread_topic=str(data.get("thread_topic", "") or ""),
        latest_focus=str(data.get("latest_focus", "") or ""),
        handoffs=[
            ThreadHandoffState(
                person_id=str(item.get("person_id", "") or ""),
                roles=[str(role) for role in item.get("roles", []) if str(role)],
                message_ts=str(item.get("message_ts", "") or ""),
                text=str(item.get("text", "") or ""),
                thread_topic=str(item.get("thread_topic", "") or ""),
                latest_focus=str(item.get("latest_focus", "") or ""),
            )
            for item in handoffs
            if isinstance(item, dict) and str(item.get("person_id", "") or "")
        ],
        system_notices=[
            ThreadSystemNoticeState(
                kind=str(item.get("kind", "") or ""),
                reason=str(item.get("reason", "failed") or "failed"),
                person_id=str(item.get("person_id", "") or ""),
                source_event_id=str(item.get("source_event_id", "") or ""),
                message_ts=str(item.get("message_ts", "") or ""),
                run_id=str(item.get("run_id", "") or ""),
                retry_after_at=str(item.get("retry_after_at", "") or ""),
                retry_after_text=str(item.get("retry_after_text", "") or ""),
                recorded_at=str(item.get("recorded_at", "") or ""),
            )
            for item in system_notices
            if isinstance(item, dict)
            and str(item.get("kind", "") or "")
            and str(item.get("source_event_id", "") or "")
        ],
        backfill_disabled_reason=str(data.get("backfill_disabled_reason", "") or ""),
        backfill_error_count=_to_non_negative_int(data.get("backfill_error_count")),
        last_backfill_error=str(data.get("last_backfill_error", "") or ""),
        # A corrupted stored level must not block the thread: it is dropped
        # so the next assessment simply starts over.
        effort=normalize_effort(data.get("effort"), strict=False),
    )


def _safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def _to_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _to_non_negative_int(value: object) -> int:
    if not isinstance(value, int | str | bytes | bytearray):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _to_str_object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if str(key)}


def _pending_metadata(value: object) -> dict[str, object]:
    """The only metadata a persisted pending event carries is GuildBotics'
    own workflow-status marker, rebuilt from its schema; everything else
    (provider metadata included) has no slot in the shared file."""
    return normalize_workflow_status_metadata(value)


def _pending_event_to_item(pending: PendingChatEvent) -> dict[str, object]:
    event = pending.event
    return {
        "event_id": event.event_id,
        "channel_id": event.channel_id,
        "message_ts": event.message_ts,
        "thread_ts": event.thread_ts,
        "author_id": event.author_id,
        "text": event.text,
        "mentions": [str(x) for x in event.mentions if str(x)],
        "is_edit_or_delete": bool(event.is_edit_or_delete),
        "is_bot_message": bool(event.is_bot_message),
        "is_thread_reply": bool(event.is_thread_reply),
        "metadata": _pending_metadata(event.metadata),
        "chat_participation": pending.chat_participation,
        "attempt_count": max(0, int(pending.attempt_count)),
        "max_attempts": max(1, int(pending.max_attempts)),
        "next_attempt_at": pending.next_attempt_at,
        "run_id": pending.run_id,
        "last_error_category": pending.last_error_category,
        "wake_cursor": pending.wake_cursor,
    }


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
