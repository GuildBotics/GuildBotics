"""The local chat: each channel a file of the workspace's device-local
``.guildbotics/local/services/chat``, reached with no credential.

A channel ``<channel_id>`` is the file ``<channel_id>.jsonl``, one JSON object
per line, appended and never rewritten: a message (``message_id``,
``thread_id``, ``occurred_at``, ``author``, ``text``, and ``bot`` and
``metadata`` when they apply) or a reaction to one (``reaction``,
``message_id``, ``author``). Reading a channel folds its reactions into the
messages they react to. A thread is named by its first message, so a message
that starts one has its own id as ``thread_id``.

A member is the user of its ``person_id``, and mentions one as
``@<person_id>``. A message is addressed by
``local://chat/<channel_id>/<message_id>``, with ``?thread=<thread_id>`` when it
is a reply.

The listener watches the files for lines appended after it starts, so a test
or an end-to-end run wakes the chat workflow by appending one message.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from logging import Logger
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from guildbotics.entities.team import Person
from guildbotics.integrations.chat_workflow_status import (
    normalize_workflow_status_metadata,
)
from guildbotics.integrations.event_listener import EventListener
from guildbotics.runtime.chat_service import (
    SEMANTIC_REACTIONS,
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatMessageRef,
    ChatPostResult,
    ChatService,
    ChatServiceError,
    ChatThreadNotFoundError,
    CredentialCheck,
)
from guildbotics.utils.fileio import get_workspace_local_path

NAME = "local"
_SCHEME = "local"
_CHANNEL = re.compile(r"[A-Za-z0-9_.-]+")
_MENTION = re.compile(r"@([a-z0-9_-]+)")
_POLL_SECONDS = 0.1


def root() -> Path:
    return get_workspace_local_path("services", "chat")


def channel_path(channel_id: str) -> Path:
    """Raises:
    ChatServiceError: If ``channel_id`` cannot name a channel.
    """
    if not _CHANNEL.fullmatch(channel_id) or channel_id in {".", ".."}:
        raise ChatServiceError(f"Unsupported local chat channel: {channel_id}")
    return root() / f"{channel_id}.jsonl"


def append(channel_id: str, line: dict[str, Any]) -> None:
    """Add one line to the channel, creating it.

    Raises:
        ChatServiceError: If the channel cannot be written.
    """
    path = channel_path(channel_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ChatServiceError(f"Local chat channel cannot be written: {exc}") from exc


def message_url(channel_id: str, message_id: str, thread_id: str = "") -> str:
    query = f"?thread={thread_id}" if thread_id and thread_id != message_id else ""
    return f"{_SCHEME}://chat/{channel_id}/{message_id}{query}"


def mentions(text: str) -> list[str]:
    return list(dict.fromkeys(_MENTION.findall(text or "")))


def _event(channel_id: str, line: dict[str, Any]) -> ChatEvent:
    message_id = str(line["message_id"])
    thread_id = str(line.get("thread_id") or message_id)
    text = str(line.get("text", ""))
    return ChatEvent(
        event_id=f"{channel_id}:{message_id}",
        channel_id=channel_id,
        message_id=message_id,
        thread_id=thread_id,
        occurred_at=datetime.fromisoformat(str(line["occurred_at"])),
        author_id=str(line.get("author", "")) or None,
        text=text,
        mentions=mentions(text),
        is_bot_message=bool(line.get("bot", False)),
        is_thread_reply=thread_id != message_id,
        metadata=normalize_workflow_status_metadata(line.get("metadata")),
    )


def _record(raw: str | bytes) -> dict[str, Any] | None:
    """The message or reaction one line records; ``None`` for a line that
    records neither (damaged, or not a record at all), which is skipped."""
    try:
        line = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(line, dict) or not isinstance(line.get("message_id"), str):
        return None
    if "reaction" in line:
        return line
    try:
        occurred_at = datetime.fromisoformat(str(line.get("occurred_at")))
    except ValueError:
        return None
    # A time without its zone is no point in time to place the message at.
    return line if occurred_at.tzinfo is not None else None


def _lines(path: Path) -> list[dict[str, Any]]:
    """The records of a channel's file, in the order written.

    Raises:
        ChatServiceError: If the file cannot be read.
    """
    try:
        raw = path.read_bytes() if path.is_file() else b""
    except OSError as exc:
        raise ChatServiceError(f"Local chat channel cannot be read: {exc}") from exc
    return [
        record for line in raw.splitlines() if (record := _record(line)) is not None
    ]


def _messages(channel_id: str) -> list[dict[str, Any]]:
    """The channel's messages in order, each with the reactions it got."""
    messages: dict[str, dict[str, Any]] = {}
    for line in _lines(channel_path(channel_id)):
        if "reaction" in line:
            target = messages.get(str(line["message_id"]))
            if target is not None:
                target["reactions"].append(
                    {"reaction": line["reaction"], "author": line.get("author", "")}
                )
        else:
            messages[str(line["message_id"])] = {**line, "reactions": []}
    return sorted(messages.values(), key=lambda m: _event(channel_id, m).position)


def _page(events: list[ChatEvent], cursor: str | None, limit: int) -> ChatEventPage:
    """Raises:
    ChatServiceError: If ``cursor`` is not one a page gave.
    """
    if not (cursor or "0").isdecimal():
        raise ChatServiceError(f"Invalid local chat cursor: {cursor}")
    start = int(cursor or 0)
    end = start + limit
    return ChatEventPage(
        events=events[start:end], cursor=str(end) if end < len(events) else None
    )


class LocalChatService(ChatService):
    """A member's chat in the local channels, as the member's own user."""

    def __init__(self, person: Person) -> None:
        self._person = person

    async def get_bot_identity(self) -> ChatIdentity:
        return ChatIdentity(
            user_id=self._person.person_id,
            display_name=self._person.name,
            workspace=NAME,
        )

    async def check_credentials(self) -> list[CredentialCheck]:
        return []

    def self_user_id(self, person: Person) -> str:
        return person.person_id

    def parse_message_url(self, url: str) -> ChatMessageRef:
        parsed = urlparse(url)
        channel_id, _, message_id = parsed.path.strip("/").partition("/")
        if parsed.scheme != _SCHEME or parsed.netloc != "chat" or not message_id:
            raise ChatServiceError(f"Unsupported local chat URL: {url}")
        channel_path(channel_id)
        thread = parse_qs(parsed.query).get("thread", [message_id])[0]
        return ChatMessageRef(
            channel_id=channel_id, message_id=message_id, thread_id=thread
        )

    def mentioned_user_ids(self, text: str) -> list[str]:
        return mentions(text)

    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        events = [
            event
            for event in (_event(channel_id, m) for m in _messages(channel_id))
            if not event.is_thread_reply
            and (since is None or event.occurred_at >= since)
            and (until is None or event.occurred_at <= until)
        ]
        return _page(events, cursor, limit)

    async def list_thread_events(
        self,
        channel_id: str,
        *,
        thread_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        events = [
            _event(channel_id, m)
            for m in _messages(channel_id)
            if str(m.get("thread_id") or m["message_id"]) == thread_id
        ]
        if not any(event.message_id == thread_id for event in events):
            raise ChatThreadNotFoundError(
                f"Local chat thread was not found: {channel_id}/{thread_id}"
            )
        return _page(events, cursor, limit)

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        name = channel_name.strip().lstrip("#")
        try:
            return name if channel_path(name).is_file() else None
        except ChatServiceError:
            return None

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        message_id = uuid4().hex
        # A channel names each message by a time of its own, after all before
        # it, as a hosted chat does: a clock that ticks coarsely (Windows)
        # would otherwise give two posts one time, ordered by random ids.
        latest = max(
            (_event(channel_id, m).occurred_at for m in _messages(channel_id)),
            default=None,
        )
        occurred_at = datetime.now(UTC)
        if latest is not None and occurred_at <= latest:
            occurred_at = latest + timedelta(microseconds=1)
        line: dict[str, Any] = {
            "message_id": message_id,
            "thread_id": thread_id or message_id,
            "occurred_at": occurred_at.isoformat(),
            "author": self._person.person_id,
            "text": text,
            "bot": True,
        }
        if metadata:
            line["metadata"] = metadata
        append(channel_id, line)
        return ChatPostResult(
            channel_id=channel_id,
            message_id=message_id,
            thread_id=thread_id or message_id,
            occurred_at=occurred_at,
        )

    async def add_reaction(
        self, channel_id: str, message_id: str, reaction: str
    ) -> None:
        if reaction not in SEMANTIC_REACTIONS:
            raise ChatServiceError(f"Unsupported semantic reaction: {reaction}")
        target = next(
            (m for m in _messages(channel_id) if m["message_id"] == message_id), None
        )
        if target is None:
            raise ChatServiceError(
                f"Local chat message was not found: {channel_id}/{message_id}"
            )
        mine = {"reaction": reaction, "author": self._person.person_id}
        # Reacting again is the same reaction, as a hosted chat keeps it.
        if mine not in target["reactions"]:
            append(channel_id, {**mine, "message_id": message_id})

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        return _MENTION.sub(
            lambda m: f"@{participant_labels.get(m.group(1), 'participant')}",
            text or "",
        )

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        user_ids = {label: user_id for user_id, label in participant_labels.items()}
        return re.sub(
            r"@([A-Za-z0-9_-]+)",
            lambda m: f"@{user_ids.get(m.group(1), m.group(1))}",
            text or "",
        )


class LocalChatListener(EventListener):
    """The messages appended to any local channel since it started."""

    def __init__(self, logger: Logger, on_activity: Callable[[], None]) -> None:
        self._logger = logger
        self._on_activity = on_activity
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._queue: list[ChatEvent] = []
        self._offsets: dict[Path, int] = {}

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def auth_failed(self) -> bool:
        return False

    def start(self) -> None:
        if self.connected:
            return
        self._stop.clear()
        self._offsets = {path: path.stat().st_size for path in self._files()}
        self._thread = threading.Thread(
            target=self._watch, name="guildbotics-local-chat-listener", daemon=True
        )
        self._thread.start()
        self._on_activity()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def drain_events(self) -> list[ChatEvent]:
        with self._lock:
            drained, self._queue = self._queue, []
        return sorted(drained, key=lambda event: event.position)

    def _files(self) -> list[Path]:
        return sorted(root().glob("*.jsonl")) if root().is_dir() else []

    def _watch(self) -> None:
        while not self._stop.wait(_POLL_SECONDS):
            arrived = [event for path in self._files() for event in self._read(path)]
            if arrived:
                with self._lock:
                    self._queue.extend(arrived)
                self._on_activity()

    def _read(self, path: Path) -> list[ChatEvent]:
        """The messages of the whole lines appended to ``path`` since it was
        last read; a line that records none is passed over, alone."""
        offset = self._offsets.get(path, 0)
        try:
            with path.open("rb") as file:
                file.seek(offset)
                data = file.read()
        except OSError as exc:
            self._logger.warning("local chat channel unreadable: %s", exc)
            return []
        complete = data[: data.rfind(b"\n") + 1]
        self._offsets[path] = offset + len(complete)
        events = []
        for line in complete.splitlines():
            record = _record(line)
            if record is None:
                if line.strip():
                    self._logger.warning("local chat line skipped: %r", line[:80])
            elif "reaction" not in record:
                events.append(_event(path.stem, record))
        return events
