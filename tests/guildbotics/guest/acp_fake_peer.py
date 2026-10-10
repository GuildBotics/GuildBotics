"""Fake ACP v1 peer plumbing shared by the native ACP adapter tests.

Only the transport belongs here: line framing, request/response correlation and
the subprocess stand-in. What an agent answers is provider behaviour, so each
adapter's tests subclass :class:`AcpPeerBase` and implement ``handle``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# Option ids are chosen by the agent per request; the kind is a separate field.
DEFAULT_OPTIONS = [
    {"optionId": "opt-a1", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "opt-r7", "name": "Reject once", "kind": "reject_once"},
    {"optionId": "opt-r9", "name": "Always reject", "kind": "reject_always"},
]


class Writer:
    """The peer's stdin: every line written is handled as a request."""

    def __init__(self, peer: AcpPeerBase) -> None:
        self.peer = peer

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            if line:
                self.peer.handle(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.peer.returncode = 0
        self.peer.stdout.feed_eof()
        self.peer.stderr.feed_eof()


class AcpPeerBase:
    """A fake agent process speaking ACP v1 over line-delimited JSON-RPC."""

    SESSION_ID = "019fad69-10a7-7931-81a0-1639a139c964"

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader(limit=2**16)
        self.stderr = asyncio.StreamReader()
        self.stdin = Writer(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []

    def handle(self, message: dict[str, Any]) -> None:
        """Answer one client message. Implemented per provider."""
        raise NotImplementedError

    def send_result(self, request_id: Any, result: Any) -> None:
        self.feed({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id: Any, error: dict[str, Any]) -> None:
        self.feed({"jsonrpc": "2.0", "id": request_id, "error": error})

    def feed(self, message: dict[str, Any]) -> None:
        # Split every message so the adapter can never rely on one read
        # returning one whole JSON document.
        encoded = (json.dumps(message) + "\n").encode()
        midpoint = max(1, len(encoded) // 2)
        self.stdout.feed_data(encoded[:midpoint])
        self.stdout.feed_data(encoded[midpoint:])

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def sent(self, method: str) -> dict[str, Any]:
        return next(
            message for message in self.messages if message.get("method") == method
        )

    def all_sent(self, method: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message.get("method") == method]

    def methods(self) -> list[str]:
        return [
            str(message["method"])
            for message in self.messages
            if "method" in message and "id" in message
        ]


def install(
    monkeypatch: pytest.MonkeyPatch, *peers: AcpPeerBase
) -> list[tuple[Any, ...]]:
    """Make the adapter's process launches return ``peers``; record the launches.

    One peer per launch, in order. A closed peer's streams are at EOF, so a test
    that expects the adapter to restart its process must supply a second peer.
    Launches past the last one keep returning it, which is what a test that
    supplies a single peer already relies on.
    """
    launched: list[tuple[Any, ...]] = []

    async def create_process(*args: Any, **kwargs: Any) -> AcpPeerBase:
        peer = peers[min(len(launched), len(peers) - 1)]
        launched.append((args, kwargs))
        return peer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    return launched


def session_update(session_id: str, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def text_chunk(text: str, message_id: str = "") -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }
    if message_id:
        update["messageId"] = message_id
    return update
