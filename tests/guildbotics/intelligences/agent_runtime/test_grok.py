from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.capabilities.task_runs import TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime.grok import (
    CLIENT_VERSION,
    GrokAcpAdapter,
    _launch_argv,
    _sandbox_profile,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.policy import AdapterFilesystemPolicy

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "grok_initialize_0_2_114.json").read_text()
)


def _initialize(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(FIXTURE)
    payload.update(overrides)
    return payload


def _update(session_id: str, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def _chunk(text: str, message_id: str = "") -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }
    if message_id:
        update["messageId"] = message_id
    return update


# Option ids are chosen by the agent per request; the kind is a separate field.
DEFAULT_OPTIONS = [
    {"optionId": "opt-a1", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "opt-r7", "name": "Reject once", "kind": "reject_once"},
    {"optionId": "opt-r9", "name": "Always reject", "kind": "reject_always"},
]


class _Writer:
    def __init__(self, peer: "_Peer") -> None:
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


class _Peer:
    """A fake ``grok agent stdio`` peer speaking ACP v1 over line JSON-RPC."""

    SESSION_ID = "019fad69-10a7-7931-81a0-1639a139c964"

    def __init__(
        self,
        *,
        initialize: dict[str, Any] | None = None,
        updates: list[dict[str, Any]] | None = None,
        stop_reason: str = "end_turn",
        replay: list[dict[str, Any]] | None = None,
        load_error: dict[str, Any] | None = None,
        authenticate_error: dict[str, Any] | None = None,
        prompt_error: dict[str, Any] | None = None,
        permission_request: dict[str, Any] | None = None,
        permission_options: list[dict[str, Any]] | None = None,
        noise: bool = False,
        prompt_delay: float = 0.0,
        turn_usage: dict[str, Any] | None = None,
        usage_channel: str = "_x.ai/session_notification",
        replay_extensions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stdout = asyncio.StreamReader(limit=2**16)
        self.stderr = asyncio.StreamReader()
        self.stdin = _Writer(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.initialize = initialize if initialize is not None else _initialize()
        self.updates = updates if updates is not None else [_chunk("hello world")]
        self.stop_reason = stop_reason
        self.replay = replay or []
        self.load_error = load_error
        self.authenticate_error = authenticate_error
        self.prompt_error = prompt_error
        self.permission_request = permission_request
        self.permission_options = (
            permission_options if permission_options is not None else DEFAULT_OPTIONS
        )
        self.noise = noise
        self.prompt_delay = prompt_delay
        self.turn_usage = turn_usage
        self.usage_channel = usage_channel
        self.replay_extensions = replay_extensions or []

    def handle(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method is None or request_id is None:
            return
        if method == "initialize":
            # Grok Build 0.2.114 rejects a clientInfo without `version` with
            # "Invalid params: missing field `version`".
            client_info = message.get("params", {}).get("clientInfo", {})
            if not str(client_info.get("version", "")):
                self._error(
                    request_id,
                    {
                        "code": -32602,
                        "message": "Invalid params",
                        "data": "missing field `version` at line 1 column 102",
                    },
                )
                return
            self._result(request_id, self.initialize)
        elif method == "authenticate":
            if self.authenticate_error:
                self._error(request_id, self.authenticate_error)
            else:
                self._result(request_id, {})
        elif method == "session/new":
            self._result(request_id, {"sessionId": self.SESSION_ID})
            self._emit_noise()
        elif method in {"session/load", "session/resume"}:
            if self.load_error:
                self._error(request_id, self.load_error)
                return
            for update in self.replay:
                self._feed(_update(self.SESSION_ID, update))
            for update in self.replay_extensions:
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "method": "_x.ai/session/update",
                        "params": {"sessionId": self.SESSION_ID, "update": update},
                    }
                )
            self._emit_noise()
            self._result(request_id, {})
        elif method == "session/prompt":
            if self.prompt_error:
                self._error(request_id, self.prompt_error)
                return
            if self.prompt_delay:
                # A real turn answers session/prompt only when the work is done.
                asyncio.get_running_loop().create_task(self._answer_later(request_id))
                return
            if self.permission_request is not None:
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "id": 9001,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": self.SESSION_ID,
                            "toolCall": self.permission_request,
                            "options": self.permission_options,
                        },
                    }
                )
            for update in self.updates:
                self._feed(_update(self.SESSION_ID, update))
            if self.turn_usage is not None:
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "method": self.usage_channel,
                        "params": {
                            "sessionId": self.SESSION_ID,
                            "update": {
                                "sessionUpdate": "turn_completed",
                                "stop_reason": self.stop_reason,
                                "usage": self.turn_usage,
                            },
                        },
                    }
                )
            self._emit_noise()
            self._result(request_id, {"stopReason": self.stop_reason})

    async def _answer_later(self, request_id: Any) -> None:
        await asyncio.sleep(self.prompt_delay)
        for update in self.updates:
            self._feed(_update(self.SESSION_ID, update))
        self._result(request_id, {"stopReason": self.stop_reason})

    def _emit_noise(self) -> None:
        if not self.noise:
            return
        self._feed(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/announcements/update",
                "params": {
                    "announcements": [
                        {"id": "promo-supergrok-upsell", "message": "Upgrade now"}
                    ]
                },
            }
        )
        self._feed(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/settings/update",
                "params": {"tips": ["Use Ctrl+O to subscribe."]},
            }
        )
        self._feed(
            _update(
                self.SESSION_ID,
                {"sessionUpdate": "available_commands_update", "availableCommands": []},
            )
        )

    def _result(self, request_id: Any, result: Any) -> None:
        self._feed({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, error: dict[str, Any]) -> None:
        self._feed({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _feed(self, message: dict[str, Any]) -> None:
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

    def methods(self) -> list[str]:
        return [
            str(message["method"])
            for message in self.messages
            if "method" in message and "id" in message
        ]


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "grok", "ticket", "issue-334")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
        resume_policy=ResumePolicy.AUTO,
        **overrides,
    )


def _install(monkeypatch: pytest.MonkeyPatch, peer: _Peer) -> list[tuple[Any, ...]]:
    launched: list[tuple[Any, ...]] = []

    async def create_process(*args: Any, **kwargs: Any) -> _Peer:
        launched.append((args, kwargs))
        return peer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    return launched


async def _run(
    adapter: GrokAcpAdapter,
    tmp_path: Path,
    conversation: ConversationRecord | None = None,
    **context_overrides: Any,
) -> tuple[Any, list[AgentEvent]]:
    context = _context(tmp_path, **context_overrides)
    record = conversation or ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []
    result = await adapter.run_turn("do the thing", context, record, events.append)
    await adapter.close()
    return result, events


# --- session lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_streams_chunks_and_reports_the_session_id(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("hello "), _chunk("world")])
    launched = _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()

    result, events = await _run(adapter, tmp_path)

    assert result.output == "hello world"
    assert result.provider_session_id == _Peer.SESSION_ID
    assert result.provider_turn_id == ""
    assert result.finish_reason == "completed"
    assert peer.methods()[:3] == ["initialize", "authenticate", "session/new"]
    assert peer.sent("session/new")["params"]["cwd"] == str(tmp_path)
    assert any(
        event.kind is AgentEventKind.PROCESS and event.name == "started"
        for event in events
    )
    argv = launched[0][0]
    assert argv == (
        "grok",
        "--no-auto-update",
        "--sandbox",
        "workspace",
        "--always-approve",
        "agent",
        "stdio",
    )


@pytest.mark.asyncio
async def test_exact_session_is_loaded_and_never_falls_back_to_latest(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id=_Peer.SESSION_ID,
    )

    result, _events = await _run(adapter, tmp_path, conversation)

    assert "session/new" not in peer.methods()
    assert peer.sent("session/load")["params"]["sessionId"] == _Peer.SESSION_ID
    assert result.provider_session_id == _Peer.SESSION_ID


@pytest.mark.asyncio
async def test_resume_capability_is_preferred_over_load(monkeypatch, tmp_path) -> None:
    initialize = _initialize()
    initialize["agentCapabilities"]["sessionCapabilities"] = {"resume": True}
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id=_Peer.SESSION_ID,
    )

    await _run(adapter, tmp_path, conversation)

    assert "session/resume" in peer.methods()
    assert "session/load" not in peer.methods()


@pytest.mark.asyncio
async def test_replayed_history_is_counted_but_never_emitted(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        replay=[
            _chunk("an answer from a previous turn"),
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "old",
                "title": "old tool",
                "kind": "execute",
            },
            {"sessionUpdate": "usage_update", "used": 4_000, "size": 500_000},
        ],
        updates=[_chunk("fresh answer")],
    )
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id=_Peer.SESSION_ID,
    )

    result, events = await _run(adapter, tmp_path, conversation)

    assert result.output == "fresh answer"
    assert "previous turn" not in result.output
    assert not [
        event
        for event in events
        if event.kind is AgentEventKind.ASSISTANT and "previous turn" in event.message
    ]
    assert not [event for event in events if event.item_id == "old"]
    rehydrated = next(event for event in events if event.name == "history_rehydrated")
    assert rehydrated.details["replayed_updates"] == 3
    assert rehydrated.details["session_method"] == "session/load"
    # The restored context snapshot survives the replay it came from.
    assert result.usage["context_used_tokens"] == 4_000


@pytest.mark.asyncio
async def test_load_failure_is_session_unavailable_and_rotates(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(load_error={"code": -32001, "message": "session not found"})
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id="missing",
    )

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, tmp_path, conversation)

    assert excinfo.value.category is AgentRuntimeErrorCategory.SESSION_UNAVAILABLE
    assert excinfo.value.rotate_session is True
    await adapter.close()


# --- assistant assembly ------------------------------------------------------


@pytest.mark.asyncio
async def test_chunks_without_message_ids_are_joined_in_arrival_order(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("one "), _chunk("two "), _chunk("three")])
    _install(monkeypatch, peer)

    result, _events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.output == "one two three"


@pytest.mark.asyncio
async def test_chunks_are_grouped_per_message_id_in_first_seen_order(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            _chunk("A1 ", "a"),
            _chunk("B1 ", "b"),
            _chunk("A2 ", "a"),
            _chunk("B2", "b"),
        ]
    )
    _install(monkeypatch, peer)

    result, _events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.output == "A1 A2 B1 B2"


@pytest.mark.asyncio
async def test_empty_terminal_output_is_a_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[])
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True


# --- tool, usage and extension events ---------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_are_classified_into_command_and_file_change(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "run tests",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"command": ["pytest", "-q"]},
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t2",
                "title": "edit file",
                "kind": "edit",
                "status": "completed",
                "locations": [{"path": "guildbotics/main.py"}],
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t3",
                "title": "think",
                "kind": "think",
            },
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    command = next(event for event in events if event.item_id == "t1")
    assert command.kind is AgentEventKind.COMMAND
    assert command.command == "pytest -q"
    assert command.name == "started"
    changed = next(event for event in events if event.item_id == "t2")
    assert changed.kind is AgentEventKind.FILE_CHANGE
    assert changed.path == "guildbotics/main.py"
    assert changed.name == "updated"
    generic = next(event for event in events if event.item_id == "t3")
    assert generic.kind is AgentEventKind.TOOL


@pytest.mark.asyncio
async def test_context_usage_is_absolute_and_cost_stays_out_of_usage(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "usage_update",
                "used": 120_000,
                "size": 500_000,
                "cost": {"amount": "0.42", "currency": "USD"},
            },
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.usage == {
        "context_used_tokens": 120_000,
        "context_size_tokens": 500_000,
    }
    assert "input_tokens" not in result.usage
    usage_event = next(event for event in events if event.kind is AgentEventKind.USAGE)
    assert usage_event.details["cost"] == {"amount": "0.42", "currency": "USD"}
    assert "cost" not in usage_event.usage


@pytest.mark.asyncio
async def test_malformed_context_usage_is_not_treated_as_a_snapshot(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {"sessionUpdate": "usage_update", "used": "many", "size": None},
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert "context_used_tokens" not in result.usage
    assert not [event for event in events if event.kind is AgentEventKind.USAGE]


@pytest.mark.asyncio
async def test_falling_context_usage_is_detected_as_compaction(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {"sessionUpdate": "usage_update", "used": 400_000, "size": 500_000},
            {"sessionUpdate": "usage_update", "used": 90_000, "size": 500_000},
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    compaction = next(event for event in events if event.name == "context_compaction")
    assert compaction.kind is AgentEventKind.TURN
    assert compaction.details["detected_by"] == "usage_decrease"


@pytest.mark.asyncio
async def test_xai_compaction_extension_is_normalized(monkeypatch, tmp_path) -> None:
    peer = _Peer(updates=[_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "_x.ai/session_notification",
                    "params": {
                        "sessionId": _Peer.SESSION_ID,
                        "update": {
                            "sessionUpdate": "auto_compact_started",
                            "tokens_used": 460_000,
                            "context_window": 500_000,
                        },
                    },
                }
            )

    peer.handle = handle  # type: ignore[method-assign]
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    compaction = next(event for event in events if event.name == "context_compaction")
    assert compaction.details["detected_by"] == "auto_compact_started"


@pytest.mark.asyncio
async def test_xai_retry_state_reports_a_structured_rate_limit(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "_x.ai/session_notification",
                    "params": {
                        "sessionId": _Peer.SESSION_ID,
                        "update": {
                            "sessionUpdate": "retry_state",
                            "retryState": {
                                "is_rate_limited": True,
                                "exhausted": False,
                                "error_type": "rate_limit",
                                "max_retries": 3,
                            },
                        },
                    },
                }
            )

    peer.handle = handle  # type: ignore[method-assign]
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    limited = next(event for event in events if event.name == "rate_limited")
    assert limited.kind is AgentEventKind.FAILED
    assert limited.details["error_type"] == "rate_limit"


@pytest.mark.asyncio
async def test_extension_noise_is_summarized_once_instead_of_per_message(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(noise=True, updates=[_chunk("done")])
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    # Nothing from the promo channel reaches the per-event transcript.
    assert not [
        event
        for event in events
        if event.name != "protocol_extensions"
        and "Upgrade now"
        in json.dumps({"message": event.message, "details": event.details})
    ]
    summaries = [event for event in events if event.name == "protocol_extensions"]
    assert len(summaries) == 1
    unhandled = summaries[0].details["unhandled"]
    # Known peer-UI channels stay countable but keep no payload: their samples
    # would echo the prompt text, the workspace path and promotional copy.
    assert unhandled["_x.ai/announcements/update"] == {"count": 2}
    # The peer's own UI affordances never become transcript events.
    assert not [event for event in events if event.name == "unknown_update"]


@pytest.mark.asyncio
async def test_an_unrecognized_extension_channel_records_names_not_values(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "_x.ai/quota/update",
                    "params": {
                        "remaining": 3,
                        "resets_at": "2026-08-01T00:00:00Z",
                        "access_token": "secret-value",
                    },
                }
            )

    peer.handle = handle  # type: ignore[method-assign]
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    summary = next(event for event in events if event.name == "protocol_extensions")
    unhandled = summary.details["unhandled"]
    # The new channel stays discoverable by name, count and shape.
    assert unhandled["_x.ai/quota/update"]["count"] == 1
    assert unhandled["_x.ai/quota/update"]["fields"] == [
        "access_token",
        "remaining",
        "resets_at",
    ]
    # No payload value is retained: the diagnostics redactor cannot reach inside
    # a serialized payload, so a secret would otherwise survive verbatim.
    assert "secret-value" not in json.dumps(summary.details)
    assert "2026-08-01" not in json.dumps(summary.details)


@pytest.mark.asyncio
async def test_unknown_standard_update_is_recorded_as_an_event(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[{"sessionUpdate": "brand_new_update", "detail": 1}, _chunk("done")]
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    unknown = next(event for event in events if event.name == "unknown_update")
    assert unknown.details["session_update"] == "brand_new_update"


# --- stop reasons ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "category"),
    [
        ("cancelled", AgentRuntimeErrorCategory.CANCELLED),
        ("max_tokens", AgentRuntimeErrorCategory.PROCESS),
        ("max_turn_requests", AgentRuntimeErrorCategory.PROCESS),
        ("refusal", AgentRuntimeErrorCategory.PROCESS),
        ("something_new", AgentRuntimeErrorCategory.PROTOCOL),
        ("", AgentRuntimeErrorCategory.PROTOCOL),
    ],
)
@pytest.mark.asyncio
async def test_non_end_turn_stop_reasons_fail_and_rotate(
    monkeypatch, tmp_path, stop_reason: str, category: AgentRuntimeErrorCategory
) -> None:
    peer = _Peer(stop_reason=stop_reason, updates=[_chunk("partial answer")])
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is category
    assert excinfo.value.rotate_session is True
    assert "partial answer" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_partial_output_is_still_recorded_as_events_on_failure(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(stop_reason="max_tokens", updates=[_chunk("partial answer")])
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    events: list[AgentEvent] = []
    context = _context(tmp_path)

    with pytest.raises(AgentRuntimeError):
        await adapter.run_turn(
            "do the thing",
            context,
            ConversationRecord(key=context.conversation_key),
            events.append,
        )
    await adapter.close()

    assert any("partial answer" in event.message for event in events)


# --- capability gate and authentication -------------------------------------


@pytest.mark.asyncio
async def test_non_v1_protocol_is_unsupported(monkeypatch, tmp_path) -> None:
    peer = _Peer(initialize=_initialize(protocolVersion=2))
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_missing_exact_resume_capability_is_unsupported(
    monkeypatch, tmp_path
) -> None:
    initialize = _initialize()
    initialize["agentCapabilities"]["loadSession"] = False
    initialize["agentCapabilities"]["sessionCapabilities"] = {}
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert excinfo.value.details["agent_version"] == "0.2.114"


@pytest.mark.asyncio
async def test_cached_token_is_selected_from_the_advertised_methods(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert peer.sent("authenticate")["params"] == {"methodId": "cached_token"}
    started = next(event for event in events if event.name == "started")
    assert started.details["auth_method"] == "cached_token"
    assert started.details["agent_version"] == "0.2.114"


@pytest.mark.asyncio
async def test_interactive_only_auth_is_refused_with_login_guidance(
    monkeypatch, tmp_path
) -> None:
    initialize = _initialize()
    initialize["authMethods"] = [{"id": "grok.com", "name": "Grok"}]
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "grok login" in str(excinfo.value)
    assert "authenticate" not in peer.methods()
    # The interactive method is never offered back as an option.
    assert excinfo.value.details["advertised_methods"] == []


@pytest.mark.asyncio
async def test_api_key_is_used_only_when_advertised(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XAI_API_KEY", "secret-value")
    initialize = _initialize()
    initialize["authMethods"] = [{"id": "xai.api_key", "name": "API key"}]
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert peer.sent("authenticate")["params"] == {"methodId": "xai.api_key"}
    # Only the method identifier is recorded, never the key itself.
    assert "secret-value" not in json.dumps([event.details for event in events])


@pytest.mark.asyncio
async def test_authenticate_failure_is_an_authentication_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(authenticate_error={"code": -32000, "message": "token expired"})
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "grok login" in str(excinfo.value)


@pytest.mark.asyncio
async def test_structured_rate_limit_error_is_classified(monkeypatch, tmp_path) -> None:
    peer = _Peer(
        prompt_error={
            "code": -32003,
            "message": "slow down",
            "data": {"code": "usage_limit_reached", "reset_at": "2026-07-30T00:00:00Z"},
        }
    )
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert excinfo.value.details["retry_after_at"] == "2026-07-30T00:00:00Z"


# --- permissions, sandbox and isolation -------------------------------------


@pytest.mark.asyncio
async def test_unexpected_permission_request_is_declined(monkeypatch, tmp_path) -> None:
    peer = _Peer(permission_request={"title": "rm -rf /"}, updates=[_chunk("done")])
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    response = next(message for message in peer.messages if message.get("id") == 9001)
    # The agent's own id for its reject-once option, not the option kind.
    assert response["result"]["outcome"] == {
        "outcome": "selected",
        "optionId": "opt-r7",
    }
    decision = next(
        event
        for event in events
        if event.kind is AgentEventKind.APPROVAL and event.name == "decision"
    )
    assert decision.approval == "decline"
    assert decision.details["tool_call"] == "rm -rf /"


@pytest.mark.asyncio
async def test_undeclared_client_capabilities_are_refused(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer._feed(
                {
                    "jsonrpc": "2.0",
                    "id": 8001,
                    "method": "fs/write_text_file",
                    "params": {"path": "/etc/passwd"},
                }
            )

    peer.handle = handle  # type: ignore[method-assign]
    _install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    refusal = next(message for message in peer.messages if message.get("id") == 8001)
    assert refusal["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_initialize_declares_no_client_capabilities(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    _install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    params = peer.sent("initialize")["params"]
    assert params["clientCapabilities"] == {}
    assert params["protocolVersion"] == 1
    assert params["clientInfo"]["name"] == "guildbotics"
    # Required by ACP; Grok rejects initialize without it.
    assert params["clientInfo"]["version"] == CLIENT_VERSION


@pytest.mark.asyncio
async def test_host_policy_disables_the_sandbox(monkeypatch, tmp_path) -> None:
    peer = _Peer()
    launched = _install(monkeypatch, peer)
    adapter = GrokAcpAdapter(policy=AdapterFilesystemPolicy(filesystem_access="host"))

    await _run(adapter, tmp_path)

    assert launched[0][0][2:4] == ("--sandbox", "off")


@pytest.mark.asyncio
async def test_read_only_turns_keep_the_confined_sandbox(monkeypatch, tmp_path) -> None:
    peer = _Peer()
    launched = _install(monkeypatch, peer)
    adapter = GrokAcpAdapter(policy=AdapterFilesystemPolicy(filesystem_access="host"))

    await _run(adapter, tmp_path, read_only=True)

    assert launched[0][0][2:4] == ("--sandbox", "workspace")


@pytest.mark.asyncio
async def test_write_credentials_are_not_inherited(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    peer = _Peer()
    launched = _install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    env = launched[0][1]["env"]
    assert "GH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env[TASK_RUN_ENV] == "run-1"
    assert launched[0][1]["start_new_session"] is True


@pytest.mark.asyncio
async def test_cancellation_terminates_the_process_group(monkeypatch, tmp_path) -> None:
    peer = _Peer()
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    terminated: list[Any] = []

    async def terminate(process: Any) -> None:
        terminated.append(process)
        process.returncode = -15

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.grok.terminate_process_tree",
        terminate,
    )
    adapter._transport._process = peer  # type: ignore[assignment]
    adapter._active_session_id = _Peer.SESSION_ID

    await adapter.interrupt()

    assert terminated == [peer]
    assert peer.sent("session/cancel")["params"]["sessionId"] == _Peer.SESSION_ID


# --- pure helpers ------------------------------------------------------------


def test_sandbox_profile_maps_the_public_policy_values() -> None:
    assert _sandbox_profile(AdapterFilesystemPolicy("workspace")) == "workspace"
    assert _sandbox_profile(AdapterFilesystemPolicy("host")) == "off"
    assert _sandbox_profile(AdapterFilesystemPolicy("host"), read_only=True) == (
        "workspace"
    )


def test_launch_argv_places_global_flags_before_the_subcommand() -> None:
    argv = _launch_argv("grok", AdapterFilesystemPolicy("workspace"), False)

    assert argv[-2:] == ("agent", "stdio")
    assert argv.index("--no-auto-update") < argv.index("agent")
    assert argv.index("--sandbox") < argv.index("agent")
    assert argv.index("--always-approve") < argv.index("agent")


# --- observed 0.2.114 behaviour ---------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_chunks_never_reach_the_reply(monkeypatch, tmp_path) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "The user wants a short answer."},
            },
            _chunk("OK"),
        ]
    )
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.output == "OK"
    # The reasoning is still available to full-detail transcripts.
    thinking = next(event for event in events if event.name == "thinking")
    assert thinking.message == "The user wants a short answer."


@pytest.mark.parametrize(
    "channel", ["_x.ai/session_notification", "_x.ai/session/update"]
)
@pytest.mark.asyncio
async def test_turn_completed_usage_is_normalized_from_either_channel(
    monkeypatch, tmp_path, channel: str
) -> None:
    peer = _Peer(
        updates=[_chunk("OK")],
        usage_channel=channel,
        turn_usage={
            "inputTokens": 12593,
            "outputTokens": 19,
            "totalTokens": 12612,
            "cachedReadTokens": 1024,
            "reasoningTokens": 18,
            "modelCalls": 1,
            "apiDurationMs": 1815,
            "costUsdTicks": 235592000,
        },
    )
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.usage == {
        "input_tokens": 12593,
        "output_tokens": 19,
        "cached_input_tokens": 1024,
        "reasoning_output_tokens": 18,
        "total_tokens": 12612,
    }
    usage_event = next(event for event in events if event.name == "turn")
    # Cost and timing are not token counts.
    assert usage_event.details["costUsdTicks"] == 235592000
    assert "costUsdTicks" not in usage_event.usage
    assert "apiDurationMs" not in usage_event.usage
    # A usage-carrying turn is no longer summarized as an unknown extension.
    summaries = [event for event in events if event.name == "protocol_extensions"]
    assert not [
        key
        for summary in summaries
        for key in summary.details["unhandled"]
        if "turn_completed" in key
    ]


@pytest.mark.asyncio
async def test_turn_completed_without_usage_is_not_a_usage_event(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[_chunk("OK")], turn_usage={})
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.usage == {}
    assert not [event for event in events if event.kind is AgentEventKind.USAGE]


@pytest.mark.asyncio
async def test_replayed_extension_usage_is_history_not_this_turn(
    monkeypatch, tmp_path
) -> None:
    """session/load replays the previous turn's usage on the extension channel."""
    peer = _Peer(
        replay=[_chunk("the previous answer")],
        replay_extensions=[
            {
                "sessionUpdate": "turn_completed",
                "stop_reason": "end_turn",
                "usage": {"inputTokens": 999_999, "outputTokens": 111},
            }
        ],
        updates=[_chunk("fresh")],
        turn_usage={"inputTokens": 12_641, "outputTokens": 19},
    )
    _install(monkeypatch, peer)
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id=_Peer.SESSION_ID,
    )

    result, events = await _run(GrokAcpAdapter(), tmp_path, conversation)

    # The replayed turn's tokens must not be reported as this turn's.
    assert result.usage == {"input_tokens": 12_641, "output_tokens": 19}
    rehydrated = next(event for event in events if event.name == "history_rehydrated")
    assert rehydrated.details["replayed_updates"] == 2
    # Replayed history is not a newly discovered extension channel.
    summaries = [event for event in events if event.name == "protocol_extensions"]
    assert not [
        key
        for summary in summaries
        for key in summary.details["unhandled"]
        if key.startswith("_x.ai/session")
    ]


@pytest.mark.asyncio
async def test_permission_request_without_a_reject_option_is_cancelled(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        permission_request={"title": "rm -rf /"},
        permission_options=[
            {"optionId": "opt-a1", "name": "Allow once", "kind": "allow_once"}
        ],
        updates=[_chunk("done")],
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    response = next(message for message in peer.messages if message.get("id") == 9001)
    # Never fall back to an allow option when no rejection is offered.
    assert response["result"]["outcome"] == {"outcome": "cancelled"}
    decision = next(event for event in events if event.name == "decision")
    assert decision.approval == "decline"
    assert decision.details["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_reject_once_is_preferred_over_reject_always(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        permission_request={"title": "rm -rf /"},
        permission_options=[
            {"optionId": "opt-r9", "name": "Always reject", "kind": "reject_always"},
            {"optionId": "opt-r7", "name": "Reject once", "kind": "reject_once"},
        ],
        updates=[_chunk("done")],
    )
    _install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    response = next(message for message in peer.messages if message.get("id") == 9001)
    # A one-off decline must not be memorized by the agent.
    assert response["result"]["outcome"]["optionId"] == "opt-r7"


@pytest.mark.asyncio
async def test_read_tools_are_not_reported_as_file_changes(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t-read",
                "title": "read config",
                "kind": "read",
                "locations": [{"path": "guildbotics/main.py"}],
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t-search",
                "title": "grep",
                "kind": "search",
                "locations": [{"path": "guildbotics/cli/__init__.py"}],
            },
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    read = next(event for event in events if event.item_id == "t-read")
    search = next(event for event in events if event.item_id == "t-search")
    assert read.kind is AgentEventKind.TOOL
    assert search.kind is AgentEventKind.TOOL
    # The touched paths are still recorded, they just do not classify the call.
    assert read.details["paths"] == ["guildbotics/main.py"]


@pytest.mark.parametrize(
    ("tool_kind", "expected"),
    [
        ("edit", AgentEventKind.FILE_CHANGE),
        ("delete", AgentEventKind.FILE_CHANGE),
        ("move", AgentEventKind.FILE_CHANGE),
        ("execute", AgentEventKind.COMMAND),
        ("fetch", AgentEventKind.TOOL),
    ],
)
@pytest.mark.asyncio
async def test_partial_tool_updates_keep_the_kind_declared_at_start(
    monkeypatch, tmp_path, tool_kind: str, expected: AgentEventKind
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "work",
                "kind": tool_kind,
                "status": "in_progress",
            },
            # A completion update may carry only the id and the new status.
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "status": "completed",
            },
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    updates = [event for event in events if event.item_id == "t1"]
    assert [event.kind for event in updates] == [expected, expected]
    assert updates[1].details["tool_kind"] == tool_kind
    assert updates[1].details["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_kinds_do_not_leak_between_turns(monkeypatch, tmp_path) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "status": "completed",
            },
            _chunk("done"),
        ]
    )
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    adapter._tool_kinds["t1"] = "edit"

    _result, events = await _run(adapter, tmp_path)

    orphan = next(event for event in events if event.item_id == "t1")
    # A stale id from an earlier turn must not classify this turn's call.
    assert orphan.kind is AgentEventKind.TOOL
    assert orphan.details["tool_kind"] == "unknown"


@pytest.mark.asyncio
async def test_a_long_turn_is_not_cut_off_by_the_request_timeout(
    monkeypatch, tmp_path
) -> None:
    """session/prompt answers at turn end, so it must not be request-bounded."""
    peer = _Peer(updates=[_chunk("finally done")], prompt_delay=0.2)
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    # Every other request stays bounded; only the turn itself is open-ended.
    adapter._transport._request_timeout = 0.02

    result, _events = await _run(adapter, tmp_path)

    assert result.output == "finally done"


@pytest.mark.asyncio
async def test_other_requests_still_honour_the_request_timeout(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    adapter._transport.start(peer)  # type: ignore[arg-type]
    adapter._transport._request_timeout = 0.02

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter._transport.request("session/never_answered", {})
    await adapter.close()

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert "timed out" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_turn_timeout_bounds_a_stalled_prompt(monkeypatch, tmp_path) -> None:
    """The unbounded prompt request is still bounded by the turn deadline."""
    peer = _Peer(updates=[_chunk("too late")], prompt_delay=5.0)
    _install(monkeypatch, peer)
    terminated: list[Any] = []

    async def terminate(process: Any) -> None:
        terminated.append(process)
        process.returncode = -15

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.grok.terminate_process_tree",
        terminate,
    )
    adapter = GrokAcpAdapter(timeout=0.15)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert "timed out" in str(excinfo.value)
    assert excinfo.value.rotate_session is True
    # The stalled peer is stopped rather than left running.
    assert terminated == [peer]
    assert peer.sent("session/cancel")["params"]["sessionId"] == _Peer.SESSION_ID


@pytest.mark.asyncio
async def test_a_finished_turn_ends_with_the_whole_reply(monkeypatch, tmp_path) -> None:
    """The display layer drops per-chunk records only once `completed` arrives."""
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "reasoning"},
            },
            _chunk("Hel"),
            _chunk("lo"),
        ]
    )
    _install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assistant = [event for event in events if event.kind is AgentEventKind.ASSISTANT]
    assert [event.name for event in assistant] == [
        "thinking",
        "delta",
        "delta",
        "completed",
    ]
    # The terminal record carries the reply only, never the reasoning.
    assert assistant[-1].message == "Hello"
    assert result.output == "Hello"


@pytest.mark.asyncio
async def test_a_failed_turn_emits_no_completed_record(monkeypatch, tmp_path) -> None:
    peer = _Peer(stop_reason="max_tokens", updates=[_chunk("partial")])
    _install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    events: list[AgentEvent] = []
    context = _context(tmp_path)

    with pytest.raises(AgentRuntimeError):
        await adapter.run_turn(
            "do the thing",
            context,
            ConversationRecord(key=context.conversation_key),
            events.append,
        )
    await adapter.close()

    # Without it the display keeps the partial chunks, which is what happened.
    assert not [event for event in events if event.name == "completed"]


@pytest.mark.asyncio
async def test_resume_advertised_as_false_is_not_used(monkeypatch, tmp_path) -> None:
    """A peer that explicitly disables resume must still be loaded, not resumed."""
    initialize = _initialize()
    initialize["agentCapabilities"]["sessionCapabilities"] = {"resume": False}
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id=_Peer.SESSION_ID,
    )

    _result, events = await _run(GrokAcpAdapter(), tmp_path, conversation)

    assert "session/load" in peer.methods()
    assert "session/resume" not in peer.methods()
    started = next(event for event in events if event.name == "started")
    assert started.details["resume_session"] is False


@pytest.mark.asyncio
async def test_disabled_resume_without_load_session_is_unsupported(
    monkeypatch, tmp_path
) -> None:
    initialize = _initialize()
    initialize["agentCapabilities"]["loadSession"] = False
    initialize["agentCapabilities"]["sessionCapabilities"] = {"resume": False}
    peer = _Peer(initialize=initialize)
    _install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
