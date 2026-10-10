"""Each chat provider of the factory, ready for the chat port's contract.

A provider's harness gives the chat of member ``aiko`` in a project whose
chat is that provider, with channel ``dev-chat``, and what a contract cannot do
through the port: a person writing a message, the URL of a message, the
reactions a message has, and the credentials the provider should report.
Slack answers from :class:`SlackDouble`, a Slack Web API and Socket Mode that
keep what they are sent; ``local`` keeps its files under the test's workspace.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Any
from urllib.parse import parse_qs

import httpx

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.integrations.local import chat as local_chat
from guildbotics.runtime.chat_service import ChatService, CredentialCheck
from tests.guildbotics.local_chat import lines

MEMBER = "aiko"
CHANNEL_NAME = "dev-chat"


@dataclass
class ChatHarness:
    name: str
    person: Person
    team: Team
    service: ChatService
    #: The id of ``dev-chat``.
    channel_id: str
    #: Have a person write ``text`` in ``dev-chat`` (as a reply in a thread
    #: when one is given); the id of the message.
    write: Callable[..., str]
    #: The URL of a message of ``dev-chat``, of a thread when given.
    url: Callable[..., str]
    #: The reactions of a message of ``dev-chat``, as ``(reaction, user)``.
    reactions: Callable[[str], list[tuple[str, str]]]
    #: What checking the member's credentials should find.
    credentials: list[CredentialCheck]
    #: Ways the provider fails underneath, by name: each makes it fail so.
    failures: dict[str, Callable[[], None]]

    async def aclose(self) -> None:
        close = getattr(self.service, "aclose", None)
        if close is not None:
            await close()


def chat_harness(name: str, monkeypatch) -> ChatHarness:
    return {"local": _local, "slack": _slack}[name](monkeypatch)


def _team(name: str) -> tuple[Person, Team]:
    person = Person(
        person_id=MEMBER,
        name="Aiko",
        person_type="agent",
        message_channels=[{"name": CHANNEL_NAME, "chat": {"enabled": True}}],
    )
    project = Project(name="demo", services={"chat_service": {"name": name}})
    return person, Team(project=project, members=[person])


def _service(person: Person, team: Team) -> ChatService:
    return ServiceIntegrationFactory().create_chat_service(
        logging.getLogger(__name__), person, team
    )


# -- local ---------------------------------------------------------------


def _local(monkeypatch) -> ChatHarness:
    person, team = _team("local")
    ids = count(1)
    local_chat.append(
        CHANNEL_NAME,
        {
            "message_id": "welcome",
            "thread_id": "welcome",
            "occurred_at": "2026-10-01T00:00:00+00:00",
            "author": "otota",
            "text": "welcome",
        },
    )

    def write(text: str, thread_id: str = "") -> str:
        number = next(ids)
        message_id = f"p{number}"
        local_chat.append(
            CHANNEL_NAME,
            {
                "message_id": message_id,
                "thread_id": thread_id or message_id,
                "occurred_at": f"2026-10-02T00:00:{number:02d}+00:00",
                "author": "otota",
                "text": text,
            },
        )
        return message_id

    def reactions(message_id: str) -> list[tuple[str, str]]:
        return [
            (line["reaction"], line["author"])
            for line in lines(CHANNEL_NAME)
            if "reaction" in line and line["message_id"] == message_id
        ]

    return ChatHarness(
        "local",
        person,
        team,
        _service(person, team),
        CHANNEL_NAME,
        write,
        lambda message_id, thread_id="": local_chat.message_url(
            CHANNEL_NAME, message_id, thread_id
        ),
        reactions,
        [],
        {"channel unwritable": _unwritable},
    )


def _unwritable() -> None:
    """Have a directory where ``dev-chat``'s file is."""
    path = local_chat.channel_path(CHANNEL_NAME)
    path.unlink()
    path.mkdir()


# -- Slack ---------------------------------------------------------------


def _slack(monkeypatch) -> ChatHarness:
    double = SlackDouble()
    serve(double, monkeypatch)
    person, team = _team("slack")

    def url(message_id: str, thread_id: str = "") -> str:
        link = f"https://acme.slack.com/archives/C1/p{message_id.replace('.', '')}"
        return f"{link}?thread_ts={thread_id}&cid=C1" if thread_id else link

    return ChatHarness(
        "slack",
        person,
        team,
        _service(person, team),
        "C1",
        double.write,
        url,
        lambda ts: [(r, u) for r, u in double.reactions.get(ts, [])],
        [CredentialCheck("bot_token", "ok"), CredentialCheck("app_token", "ok")],
        {
            failure: (lambda failure=failure: setattr(double, "failure", failure))
            for failure in SlackDouble.FAILURES
        },
    )


def serve(double: SlackDouble, monkeypatch) -> None:
    """Have the member's Slack -- Web API and Socket Mode -- talk to
    ``double``, with both tokens for ``MEMBER``."""
    from guildbotics.integrations.slack import slack_chat_service, slack_socket_listener

    transport = httpx.MockTransport(double.respond)
    async_client, client = httpx.AsyncClient, httpx.Client

    def AsyncClient(*args, **kwargs):  # noqa: N802
        return async_client(*args, **{**kwargs, "transport": transport})

    def Client(*args, **kwargs):  # noqa: N802
        return client(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(slack_chat_service.httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(slack_socket_listener.httpx, "Client", Client)
    monkeypatch.setattr("websockets.sync.client.connect", lambda url: double.connect())
    monkeypatch.setenv(f"{MEMBER.upper()}_SLACK_BOT_TOKEN", "xoxb-aiko")
    monkeypatch.setenv(f"{MEMBER.upper()}_SLACK_APP_TOKEN", "xapp-aiko")


class _Socket:
    """The Socket Mode connection, which hands over what Slack sends."""

    def __init__(self) -> None:
        self.frames: queue.Queue[str] = queue.Queue()
        self.acks: list[str] = []
        self.closed = threading.Event()

    def recv(self) -> str:
        while not self.closed.is_set():
            try:
                return self.frames.get(timeout=0.05)
            except queue.Empty:
                continue
        raise RuntimeError("socket closed")

    def send(self, text: str) -> None:
        self.acks.append(text)

    def close(self) -> None:
        self.closed.set()


class SlackDouble:
    """Slack, as far as the chat contract asks it: it keeps the messages and
    reactions it is sent, and delivers what a person writes on the socket."""

    BOT = "U_AIKO"
    #: How Slack can fail to answer: what ``failure`` may be set to.
    FAILURES = ("unreachable", "HTTP 429", "no JSON", "no JSON object")

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = [
            {"ts": "1728000000.000001", "user": "U_OTOTA", "text": "welcome"}
        ]
        self.reactions: dict[str, list[tuple[str, str]]] = {}
        self.socket: _Socket | None = None
        self.failure: str | None = None
        self._ts = count(2)

    def connect(self) -> _Socket:
        self.socket = _Socket()
        return self.socket

    def write(self, text: str, thread_id: str = "") -> str:
        message = self._message({"user": "U_OTOTA", "text": text}, thread_id)
        envelope = {
            "type": "events_api",
            "envelope_id": f"env-{message['ts']}",
            "payload": {"event": {"type": "message", "channel": "C1", **message}},
        }
        # Socket Mode delivers only to a connection that is open.
        if self.socket is not None and not self.socket.closed.is_set():
            self.socket.frames.put(json.dumps(envelope))
        return message["ts"]

    def respond(self, request: httpx.Request) -> httpx.Response:
        if self.failure == "unreachable":
            raise httpx.ConnectError("no route to Slack", request=request)
        if self.failure == "HTTP 429":
            return httpx.Response(429, headers={"Retry-After": "1"})
        if self.failure == "no JSON":
            return httpx.Response(200, text="<html>bad gateway</html>")
        if self.failure == "no JSON object":
            return httpx.Response(200, json=["ok"])
        method = request.url.path.rsplit("/", 1)[1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        return httpx.Response(200, json={"ok": True, **self._answer(method, form)})

    def _answer(self, method: str, form: dict[str, str]) -> dict[str, Any]:
        if method == "auth.test":
            return {"user_id": self.BOT, "user": "aiko-bot", "team": "acme"}
        if method == "apps.connections.open":
            return {"url": "wss://double/socket"}
        if method == "conversations.list":
            return {"channels": [{"id": "C1", "name": CHANNEL_NAME}]}
        if method == "chat.postMessage":
            metadata = json.loads(form["metadata"]) if "metadata" in form else None
            message = self._message(
                {"user": self.BOT, "bot_id": "B1", "text": form["text"]},
                form.get("thread_ts", ""),
                metadata,
            )
            return {"ts": message["ts"]}
        if method == "reactions.add":
            given = (form["name"], self.BOT)
            if given in self.reactions.setdefault(form["timestamp"], []):
                return {"ok": False, "error": "already_reacted"}
            self.reactions[form["timestamp"]].append(given)
            return {}
        if method == "conversations.history":
            oldest, latest = form.get("oldest", "0"), form.get("latest", "9" * 12)
            return {
                "messages": [
                    m
                    for m in reversed(self.messages)
                    if m.get("thread_ts", m["ts"]) == m["ts"]
                    and float(oldest) <= float(m["ts"]) <= float(latest)
                ]
            }
        if method == "conversations.replies":
            thread = [
                m for m in self.messages if m.get("thread_ts", m["ts"]) == form["ts"]
            ]
            if not any(m["ts"] == form["ts"] for m in thread):
                return {"ok": False, "error": "thread_not_found"}
            return {"messages": thread}
        raise AssertionError(f"unexpected Slack method: {method}")

    def _message(
        self, fields: dict[str, Any], thread_ts: str, metadata: dict | None = None
    ) -> dict[str, Any]:
        message = {"type": "message", "ts": f"1728000000.{next(self._ts):06d}"}
        message |= fields
        if thread_ts:
            message["thread_ts"] = thread_ts
        if metadata:
            message["metadata"] = metadata
        self.messages.append(message)
        return message
