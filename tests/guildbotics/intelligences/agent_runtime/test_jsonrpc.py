from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime.jsonrpc import (
    FATAL_NOTIFICATION,
    METHOD_NOT_FOUND,
    LineJsonRpcTransport,
    RpcError,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)


class _Writer:
    def __init__(self, process: "_Process") -> None:
        self.process = process

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            if line:
                self.process.written.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.returncode = 0


class _Process:
    def __init__(self, *, stream_limit: int = 2**16) -> None:
        self.stdout = asyncio.StreamReader(limit=stream_limit)
        self.stderr = asyncio.StreamReader()
        self.stdin = _Writer(self)
        self.returncode: int | None = None
        self.written: list[dict[str, Any]] = []

    def feed(self, message: dict[str, Any]) -> None:
        encoded = (json.dumps(message) + "\n").encode()
        midpoint = max(1, len(encoded) // 2)
        self.stdout.feed_data(encoded[:midpoint])
        self.stdout.feed_data(encoded[midpoint:])

    def feed_raw(self, payload: bytes) -> None:
        self.stdout.feed_data(payload)

    def eof(self) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _transport(**kwargs: Any) -> tuple[LineJsonRpcTransport, _Process]:
    process = _Process(stream_limit=kwargs.pop("stream_limit", 2**16))
    transport = LineJsonRpcTransport(label="Test Peer", **kwargs)
    transport.start(process)  # type: ignore[arg-type]
    return transport, process


@pytest.mark.asyncio
async def test_codex_dialect_omits_jsonrpc_version() -> None:
    transport, process = _transport()
    task = asyncio.create_task(transport.request("ping", {"a": 1}))
    await asyncio.sleep(0)

    assert process.written == [{"method": "ping", "id": 1, "params": {"a": 1}}]
    process.feed({"id": 1, "result": "pong"})
    assert await task == "pong"
    await transport.aclose()


@pytest.mark.asyncio
async def test_acp_dialect_adds_jsonrpc_version_to_every_message() -> None:
    transport, process = _transport(include_version=True)
    task = asyncio.create_task(transport.request("initialize", {}))
    await asyncio.sleep(0)
    await transport.notify("session/cancel", {"sessionId": "s-1"})
    await transport.respond(7, result={"ok": True})

    assert [message["jsonrpc"] for message in process.written] == ["2.0"] * 3
    process.feed({"jsonrpc": "2.0", "id": 1, "result": {}})
    await task
    await transport.aclose()


@pytest.mark.asyncio
async def test_out_of_order_responses_are_correlated_by_request_id() -> None:
    transport, process = _transport()
    first = asyncio.create_task(transport.request("first", {}))
    second = asyncio.create_task(transport.request("second", {}))
    await asyncio.sleep(0)

    ids = {message["method"]: message["id"] for message in process.written}
    process.feed({"id": ids["second"], "result": "second-result"})
    process.feed({"id": ids["first"], "result": "first-result"})

    assert await first == "first-result"
    assert await second == "second-result"
    await transport.aclose()


@pytest.mark.asyncio
async def test_unknown_and_duplicate_response_ids_are_ignored() -> None:
    transport, process = _transport()
    task = asyncio.create_task(transport.request("only", {}))
    await asyncio.sleep(0)

    process.feed({"id": 999, "result": "stray"})
    process.feed({"id": 1, "result": "first"})
    process.feed({"id": 1, "result": "duplicate"})

    assert await task == "first"
    await transport.aclose()


@pytest.mark.asyncio
async def test_error_response_raises_rpc_error_with_payload() -> None:
    transport, process = _transport()
    task = asyncio.create_task(transport.request("boom", {}))
    await asyncio.sleep(0)
    process.feed({"id": 1, "error": {"code": METHOD_NOT_FOUND, "message": "nope"}})

    with pytest.raises(RpcError) as excinfo:
        await task
    assert excinfo.value.error["code"] == METHOD_NOT_FOUND
    await transport.aclose()


@pytest.mark.asyncio
async def test_notifications_are_queued_in_arrival_order() -> None:
    transport, process = _transport()
    process.feed({"method": "one", "params": {}})
    process.feed({"method": "two", "params": {}})

    assert (await transport.next_notification())["method"] == "one"
    assert (await transport.next_notification())["method"] == "two"
    await transport.aclose()


@pytest.mark.asyncio
async def test_drain_notifications_takes_only_what_already_arrived() -> None:
    transport, process = _transport()
    process.feed({"method": "one", "params": {}})
    process.feed({"method": "two", "params": {}})
    await asyncio.sleep(0)

    drained = transport.drain_notifications()

    assert [message["method"] for message in drained] == ["one", "two"]
    assert transport.drain_notifications() == []
    await transport.aclose()


@pytest.mark.asyncio
async def test_reverse_request_is_dispatched_to_the_handler() -> None:
    seen: list[tuple[str, Any, dict[str, Any]]] = []

    async def handler(method: str, request_id: Any, params: dict[str, Any]) -> None:
        seen.append((method, request_id, params))

    transport, process = _transport(on_reverse_request=handler)
    process.feed({"id": 5, "method": "session/request_permission", "params": {"a": 1}})
    await asyncio.sleep(0)

    assert seen == [("session/request_permission", 5, {"a": 1})]
    await transport.aclose()


@pytest.mark.asyncio
async def test_reverse_request_fails_closed_without_a_handler() -> None:
    transport, process = _transport()
    process.feed({"id": 5, "method": "fs/read_text_file", "params": {}})
    await asyncio.sleep(0)

    assert process.written == [
        {
            "id": 5,
            "error": {
                "code": METHOD_NOT_FOUND,
                "message": "Unsupported request: fs/read_text_file",
            },
        }
    ]
    await transport.aclose()


@pytest.mark.asyncio
async def test_malformed_json_fails_pending_requests_as_protocol_error() -> None:
    transport, process = _transport()
    task = asyncio.create_task(transport.request("hello", {}))
    await asyncio.sleep(0)
    process.feed_raw(b"not-json\n")

    with pytest.raises(AgentRuntimeError) as excinfo:
        await task
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    await transport.aclose()


@pytest.mark.asyncio
async def test_oversized_message_is_reported_as_protocol_error() -> None:
    transport, process = _transport(stream_limit=2**10)
    task = asyncio.create_task(transport.request("hello", {}))
    await asyncio.sleep(0)
    process.feed_raw(b"x" * (4 * 2**10))

    with pytest.raises(AgentRuntimeError) as excinfo:
        await task
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    await transport.aclose()


@pytest.mark.asyncio
async def test_eof_fails_pending_requests_and_publishes_a_fatal_notification() -> None:
    transport, process = _transport()
    task = asyncio.create_task(transport.request("hello", {}))
    await asyncio.sleep(0)
    process.stderr.feed_data(b"grok: something went wrong\n")
    await asyncio.sleep(0)
    process.eof()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await task
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert (await transport.next_notification())["method"] == FATAL_NOTIFICATION
    assert transport.fatal_error is not None
    assert "something went wrong" in transport.stderr_text()
    await transport.aclose()


@pytest.mark.asyncio
async def test_writing_to_a_stopped_peer_is_a_process_error() -> None:
    transport, process = _transport()
    process.returncode = 1

    with pytest.raises(AgentRuntimeError) as excinfo:
        await transport.request("hello", {})
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert "Test Peer is not running." in str(excinfo.value)
    await transport.aclose()


@pytest.mark.asyncio
async def test_request_timeout_is_reported_with_the_peer_label() -> None:
    transport, _process = _transport(request_timeout=0.01)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await transport.request("slow", {})
    assert "Test Peer request 'slow' timed out." in str(excinfo.value)
    await transport.aclose()


@pytest.mark.asyncio
async def test_stderr_collection_is_bounded() -> None:
    transport, process = _transport()
    for index in range(150):
        process.stderr.feed_data(f"line-{index}\n".encode())
    await asyncio.sleep(0)

    tail = transport.stderr_tail(2).splitlines()

    assert tail == ["line-148", "line-149"]
    assert len(transport.stderr_text().splitlines()) == 100
    await transport.aclose()


@pytest.mark.asyncio
async def test_restart_resets_request_ids_and_pending_state() -> None:
    transport, process = _transport()
    first = asyncio.create_task(transport.request("hello", {}))
    await asyncio.sleep(0)
    process.eof()
    with pytest.raises(AgentRuntimeError):
        await first
    await transport.aclose()

    restarted = _Process()
    transport.start(restarted)  # type: ignore[arg-type]
    second = asyncio.create_task(transport.request("hello", {}))
    await asyncio.sleep(0)

    assert restarted.written[0]["id"] == 1
    assert transport.fatal_error is None
    restarted.feed({"id": 1, "result": "ok"})
    assert await second == "ok"
    await transport.aclose()


@pytest.mark.asyncio
async def test_a_request_can_opt_out_of_the_deadline() -> None:
    transport, process = _transport(request_timeout=0.02)
    task = asyncio.create_task(transport.request("turn", {}, timeout=None))
    await asyncio.sleep(0.1)

    assert not task.done()
    process.feed({"id": 1, "result": "late but fine"})
    assert await task == "late but fine"
    await transport.aclose()


@pytest.mark.asyncio
async def test_a_request_can_override_the_deadline_with_its_own() -> None:
    transport, _process = _transport(request_timeout=30.0)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await transport.request("slow", {}, timeout=0.02)

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    await transport.aclose()


@pytest.mark.asyncio
async def test_an_unbounded_request_still_fails_when_the_peer_dies() -> None:
    transport, process = _transport(request_timeout=0.02)
    task = asyncio.create_task(transport.request("turn", {}, timeout=None))
    await asyncio.sleep(0)
    process.eof()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await task
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    await transport.aclose()
