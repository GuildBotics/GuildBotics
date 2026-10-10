"""Members chatting in the local chat (``guildbotics.integrations.local.chat``),
and the messages a test puts there or hands around."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.local import chat
from guildbotics.runtime.chat_service import ChatEvent

#: The time a message id that is a number of seconds counts from.
EPOCH = datetime(2026, 10, 1, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return EPOCH + timedelta(seconds=seconds)


def chat_team(*members: Person, **services: dict[str, Any]) -> Team:
    """A team whose chat is local, with ``services`` besides."""
    return Team(
        project=Project(
            name="demo",
            services={"chat_service": {"name": "local"}, **services},
        ),
        members=list(members),
    )


def chat_event(
    message_id: str = "100.1",
    *,
    channel_id: str = "C1",
    thread_id: str | None = None,
    occurred_at: datetime | None = None,
    author_id: str | None = "U_USER",
    text: str = "",
    **fields: Any,
) -> ChatEvent:
    """An event of ``message_id``, which occurred that many seconds after
    :data:`EPOCH` unless ``occurred_at`` says otherwise."""
    thread_id = thread_id or message_id
    if occurred_at is None:
        try:
            occurred_at = at(float(message_id))
        except ValueError:
            occurred_at = EPOCH
    return ChatEvent(
        event_id=fields.pop("event_id", f"{channel_id}:{message_id}"),
        channel_id=channel_id,
        message_id=message_id,
        thread_id=thread_id,
        occurred_at=occurred_at,
        author_id=author_id,
        text=text,
        is_thread_reply=fields.pop("is_thread_reply", thread_id != message_id),
        **fields,
    )


def say(
    channel_id: str,
    text: str,
    *,
    author: str = "otota",
    message_id: str,
    thread_id: str | None = None,
    occurred_at: datetime | None = None,
    bot: bool = False,
) -> ChatEvent:
    """Have ``author`` write ``text`` in the local channel, as a person
    appending a line would; the event it is."""
    line: dict[str, Any] = {
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        "author": author,
        "text": text,
    }
    if bot:
        line["bot"] = True
    chat.append(channel_id, line)
    return chat_event(
        message_id,
        channel_id=channel_id,
        thread_id=thread_id,
        occurred_at=datetime.fromisoformat(line["occurred_at"]),
        author_id=author,
        text=text,
        mentions=chat.mentions(text),
        is_bot_message=bot,
    )


def lines(channel_id: str) -> list[dict[str, Any]]:
    """Every line of the local channel, in the order written."""
    path = chat.channel_path(channel_id)
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]


def position(seconds: float) -> str:
    """The position of the message of id ``seconds`` that :func:`chat_event`
    makes."""
    return chat_event(f"{seconds}").position
