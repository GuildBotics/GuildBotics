from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from acp_fake_peer import (
    DEFAULT_OPTIONS,
    AcpPeerBase,
    install,
    session_update,
    text_chunk,
)

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime.acp import CLIENT_VERSION
from guildbotics.intelligences.agent_runtime.grok import (
    GrokAcpAdapter,
    _launch_argv,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
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
    settings_fingerprint,
)
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)

#: Run identity and delegation grant the parent process holds while a workflow
#: runs. Grok receives none of them; only the authenticated broker does.
_AMBIENT_EXECUTION_ENV = (
    RUN_ENV,
    TASK_RUN_ENV,
    LEASE_ID_ENV,
    DELEGATION_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "grok_initialize_0_2_114.json").read_text()
)


@pytest.fixture(autouse=True)
def _member_broker_without_socket(monkeypatch) -> None:
    async def start(broker: MemberCapabilityBroker) -> None:
        broker._url = "http://127.0.0.1:43123/mcp"

    monkeypatch.setattr(MemberCapabilityBroker, "_start", start)


def _initialize(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(FIXTURE)
    payload.update(overrides)
    return payload


class _Peer(AcpPeerBase):
    """A fake ``grok agent stdio`` peer speaking ACP v1 over line JSON-RPC."""

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
        super().__init__()
        self.initialize = initialize if initialize is not None else _initialize()
        self.updates = updates if updates is not None else [text_chunk("hello world")]
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
                self.send_error(
                    request_id,
                    {
                        "code": -32602,
                        "message": "Invalid params",
                        "data": "missing field `version` at line 1 column 102",
                    },
                )
                return
            self.send_result(request_id, self.initialize)
        elif method == "authenticate":
            if self.authenticate_error:
                self.send_error(request_id, self.authenticate_error)
            else:
                self.send_result(request_id, {})
        elif method == "session/new":
            self.send_result(request_id, {"sessionId": self.SESSION_ID})
            self._emit_noise()
        elif method in {"session/load", "session/resume"}:
            if self.load_error:
                self.send_error(request_id, self.load_error)
                return
            for update in self.replay:
                self.feed(session_update(self.SESSION_ID, update))
            for update in self.replay_extensions:
                self.feed(
                    {
                        "jsonrpc": "2.0",
                        "method": "_x.ai/session/update",
                        "params": {"sessionId": self.SESSION_ID, "update": update},
                    }
                )
            self._emit_noise()
            self.send_result(request_id, {})
        elif method == "session/prompt":
            if self.prompt_error:
                self.send_error(request_id, self.prompt_error)
                return
            if self.prompt_delay:
                # A real turn answers session/prompt only when the work is done.
                asyncio.get_running_loop().create_task(self._answer_later(request_id))
                return
            if self.permission_request is not None:
                self.feed(
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
                self.feed(session_update(self.SESSION_ID, update))
            if self.turn_usage is not None:
                self.feed(
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
            self.send_result(request_id, {"stopReason": self.stop_reason})

    async def _answer_later(self, request_id: Any) -> None:
        await asyncio.sleep(self.prompt_delay)
        for update in self.updates:
            self.feed(session_update(self.SESSION_ID, update))
        self.send_result(request_id, {"stopReason": self.stop_reason})

    def _emit_noise(self) -> None:
        if not self.noise:
            return
        self.feed(
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
        self.feed(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/settings/update",
                "params": {"tips": ["Use Ctrl+O to subscribe."]},
            }
        )
        self.feed(
            session_update(
                self.SESSION_ID,
                {"sessionUpdate": "available_commands_update", "availableCommands": []},
            )
        )


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "grok", "ticket", "issue-334")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
        resume_policy=ResumePolicy.AUTO,
        **overrides,
    )


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
    peer = _Peer(updates=[text_chunk("hello "), text_chunk("world")])
    launched = install(monkeypatch, peer)
    adapter = GrokAcpAdapter()

    result, events = await _run(adapter, tmp_path)

    assert result.output == "hello world"
    assert result.provider_session_id == _Peer.SESSION_ID
    assert result.provider_turn_id == ""
    assert result.finish_reason == "completed"
    assert peer.methods()[:3] == ["initialize", "authenticate", "session/new"]
    assert peer.sent("session/new")["params"]["cwd"] == str(tmp_path)
    server = peer.sent("session/new")["params"]["mcpServers"][0]
    assert server["type"] == "http"
    assert server["name"].startswith("guildbotics-member-")
    assert server["url"] == "http://127.0.0.1:43123/mcp"
    assert server["headers"][0]["name"] == "Authorization"
    assert server["headers"][0]["value"].startswith("Bearer ")
    prompt = peer.sent("session/prompt")["params"]["prompt"][0]["text"]
    assert "Use it for every" in prompt
    assert "never run those commands" in prompt
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
        "agent",
        "--always-approve",
        "stdio",
    )


@pytest.mark.asyncio
async def test_effort_settings_are_passed_as_launch_options(
    monkeypatch, tmp_path
) -> None:
    """`grok agent stdio` takes the model and reasoning effort at launch.

    They are not protocol fields, so they have to reach the process on its argv
    or they never take effect at all.
    """
    peer = _Peer(updates=[text_chunk("ok")])
    launched = install(monkeypatch, peer)
    adapter = GrokAcpAdapter()

    await _run(
        adapter,
        tmp_path,
        effort="high",
        provider_options={"model": "grok-4.5", "reasoning_effort": "high"},
    )

    argv = launched[0][0]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "grok-4.5"
    assert "--reasoning-effort" in argv
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    # These are agent options; passing them through the root parser does not
    # reliably apply them to the ACP session.
    assert argv.index("agent") < argv.index("--model") < argv.index("stdio")


@pytest.mark.asyncio
async def test_a_turn_without_effort_adds_no_launch_options(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[text_chunk("ok")])
    launched = install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    argv = launched[0][0]
    assert "--model" not in argv
    assert "--reasoning-effort" not in argv


@pytest.mark.asyncio
async def test_terminal_result_carries_the_launch_settings_as_effective(
    monkeypatch, tmp_path
) -> None:
    """The process is fixed to its launch options, so those are what it ran on."""
    peer = _Peer(updates=[text_chunk("ok")])
    install(monkeypatch, peer)

    result, _ = await _run(
        GrokAcpAdapter(),
        tmp_path,
        effort="high",
        provider_options={"model": "grok-4.5", "reasoning_effort": "high"},
    )

    assert (result.model, result.effort) == ("grok-4.5", "high")


@pytest.mark.asyncio
async def test_the_initialize_reported_model_covers_a_turn_that_imposed_none(
    monkeypatch, tmp_path
) -> None:
    """Grok names the process's current model in `initialize` `_meta.modelState`
    (observed on 0.2.114), so even a turn on the account default knows its
    model. The reasoning effort is reported nowhere and stays empty."""
    peer = _Peer(updates=[text_chunk("ok")])
    install(monkeypatch, peer)

    result, _ = await _run(GrokAcpAdapter(), tmp_path)

    assert (result.model, result.effort) == ("grok-4.5", "")


@pytest.mark.asyncio
async def test_launch_options_stand_in_when_initialize_names_no_model(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        initialize=_initialize(_meta={"agentVersion": "0.2.114"}),
        updates=[text_chunk("ok")],
    )
    install(monkeypatch, peer)

    result, _ = await _run(
        GrokAcpAdapter(),
        tmp_path,
        effort="high",
        provider_options={"model": "grok-code", "reasoning_effort": "low"},
    )

    assert (result.model, result.effort) == ("grok-code", "low")


@pytest.mark.asyncio
async def test_unknown_effort_settings_are_reported_not_silently_dropped(
    monkeypatch, tmp_path, caplog
) -> None:
    peer = _Peer(updates=[text_chunk("ok")])
    install(monkeypatch, peer)

    with caplog.at_level("WARNING"):
        await _run(
            GrokAcpAdapter(),
            tmp_path,
            effort="high",
            provider_options={"reasoning_effort": "high", "temperature": 0.9},
        )

    assert "temperature" in caplog.text


def test_only_settings_grok_can_apply_count_as_a_session_change(tmp_path) -> None:
    """The fingerprint follows what the launch actually carries."""
    adapter = GrokAcpAdapter()
    high = _context(tmp_path, effort="high", provider_options={"model": "grok-4.5"})
    low = _context(tmp_path, effort="low", provider_options={"model": "grok-4.5"})
    other = _context(tmp_path, effort="low", provider_options={"model": "grok-3"})

    # The neutral label alone changes nothing about the launch, so it is not a
    # settings change; a different model is.
    assert settings_fingerprint(adapter.applied_settings(high)) == settings_fingerprint(
        adapter.applied_settings(low)
    )
    assert settings_fingerprint(adapter.applied_settings(low)) != settings_fingerprint(
        adapter.applied_settings(other)
    )


@pytest.mark.asyncio
async def test_exact_session_is_loaded_and_never_falls_back_to_latest(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)
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
            text_chunk("an answer from a previous turn"),
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "old",
                "title": "old tool",
                "kind": "execute",
            },
            {"sessionUpdate": "usage_update", "used": 4_000, "size": 500_000},
        ],
        updates=[text_chunk("fresh answer")],
    )
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)
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
    peer = _Peer(updates=[text_chunk("one "), text_chunk("two "), text_chunk("three")])
    install(monkeypatch, peer)

    result, _events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.output == "one two three"


@pytest.mark.asyncio
async def test_chunks_are_grouped_per_message_id_in_first_seen_order(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            text_chunk("A1 ", "a"),
            text_chunk("B1 ", "b"),
            text_chunk("A2 ", "a"),
            text_chunk("B2", "b"),
        ]
    )
    install(monkeypatch, peer)

    result, _events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.output == "A1 A2 B1 B2"


@pytest.mark.asyncio
async def test_empty_terminal_output_is_a_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[])
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    compaction = next(event for event in events if event.name == "context_compaction")
    assert compaction.kind is AgentEventKind.TURN
    assert compaction.details["detected_by"] == "usage_decrease"


@pytest.mark.asyncio
async def test_xai_compaction_extension_is_normalized(monkeypatch, tmp_path) -> None:
    peer = _Peer(updates=[text_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer.feed(
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
    install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    compaction = next(event for event in events if event.name == "context_compaction")
    assert compaction.details["detected_by"] == "auto_compact_started"


@pytest.mark.asyncio
async def test_xai_retry_state_reports_a_structured_rate_limit(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[text_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer.feed(
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
    install(monkeypatch, peer)

    _result, events = await _run(GrokAcpAdapter(), tmp_path)

    limited = next(event for event in events if event.name == "rate_limited")
    assert limited.kind is AgentEventKind.FAILED
    assert limited.details["error_type"] == "rate_limit"


@pytest.mark.asyncio
async def test_extension_noise_is_summarized_once_instead_of_per_message(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(noise=True, updates=[text_chunk("done")])
    install(monkeypatch, peer)

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
    peer = _Peer(updates=[text_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer.feed(
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
    install(monkeypatch, peer)

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
        updates=[{"sessionUpdate": "brand_new_update", "detail": 1}, text_chunk("done")]
    )
    install(monkeypatch, peer)

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
    peer = _Peer(stop_reason=stop_reason, updates=[text_chunk("partial answer")])
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is category
    assert excinfo.value.rotate_session is True
    assert "partial answer" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_partial_output_is_still_recorded_as_events_on_failure(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(stop_reason="max_tokens", updates=[text_chunk("partial answer")])
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)

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
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert excinfo.value.details["agent_version"] == "0.2.114"


@pytest.mark.asyncio
async def test_missing_http_mcp_capability_is_unsupported(
    monkeypatch, tmp_path
) -> None:
    initialize = _initialize()
    initialize["agentCapabilities"]["mcpCapabilities"]["http"] = False
    peer = _Peer(initialize=initialize)
    install(monkeypatch, peer)
    adapter = GrokAcpAdapter()

    try:
        with pytest.raises(AgentRuntimeError) as excinfo:
            await _run(adapter, tmp_path)
    finally:
        await adapter.close()

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert excinfo.value.details["agent_version"] == "0.2.114"


@pytest.mark.asyncio
async def test_member_broker_start_failure_is_a_process_error(
    monkeypatch, tmp_path
) -> None:
    async def fail_to_start(_broker: MemberCapabilityBroker) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(MemberCapabilityBroker, "_start", fail_to_start)
    peer = _Peer()
    install(monkeypatch, peer)
    adapter = GrokAcpAdapter()

    try:
        with pytest.raises(AgentRuntimeError) as excinfo:
            await _run(adapter, tmp_path)
    finally:
        await adapter.close()

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS


@pytest.mark.asyncio
async def test_cached_token_is_selected_from_the_advertised_methods(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

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
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "grok login" in str(excinfo.value)
    assert "authenticate" not in peer.methods()
    # The interactive method is never offered back as an option.
    assert excinfo.value.details["advertised_methods"] == []


@pytest.mark.asyncio
async def test_api_key_auth_is_refused_with_login_guidance(
    monkeypatch, tmp_path
) -> None:
    # `XAI_API_KEY` never reaches the Grok process (the isolated environment
    # strips credential-named variables), so an API-key-only install has no
    # usable non-interactive login regardless of what the parent shell exports.
    monkeypatch.setenv("XAI_API_KEY", "secret-value")
    initialize = _initialize()
    initialize["authMethods"] = [{"id": "xai.api_key", "name": "API key"}]
    peer = _Peer(initialize=initialize)
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "grok login" in str(excinfo.value)
    assert "authenticate" not in peer.methods()
    assert excinfo.value.details["advertised_methods"] == ["xai.api_key"]
    assert "secret-value" not in json.dumps(excinfo.value.details)


@pytest.mark.asyncio
async def test_authenticate_failure_is_an_authentication_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(authenticate_error={"code": -32000, "message": "token expired"})
    install(monkeypatch, peer)

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
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert excinfo.value.details["retry_after_at"] == "2026-07-30T00:00:00Z"


@pytest.mark.asyncio
async def test_bare_protocol_error_after_rate_limit_notice_is_rate_limited(
    monkeypatch, tmp_path
) -> None:
    # Grok Build reports the rate limit through the xAI retry-state extension
    # and then fails the turn with a code-only RPC error that carries no
    # structured data of its own.
    peer = _Peer(prompt_error={"code": -32003, "message": "Internal error"})
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        if message.get("method") == "session/prompt":
            peer.feed(
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
                            },
                        },
                    },
                }
            )
        original(message)

    peer.handle = handle  # type: ignore[method-assign]
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert excinfo.value.details["error_type"] == "rate_limit"
    assert excinfo.value.details["provider_code"] == -32003


@pytest.mark.asyncio
async def test_bare_protocol_error_without_rate_limit_notice_stays_protocol(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(prompt_error={"code": -32003, "message": "Internal error"})
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.details["provider_code"] == -32003


# --- permissions, sandbox and isolation -------------------------------------


@pytest.mark.asyncio
async def test_unexpected_permission_request_is_declined(monkeypatch, tmp_path) -> None:
    peer = _Peer(permission_request={"title": "rm -rf /"}, updates=[text_chunk("done")])
    install(monkeypatch, peer)

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
    peer = _Peer(updates=[text_chunk("done")])
    original = peer.handle

    def handle(message: dict[str, Any]) -> None:
        original(message)
        if message.get("method") == "session/prompt":
            peer.feed(
                {
                    "jsonrpc": "2.0",
                    "id": 8001,
                    "method": "fs/write_text_file",
                    "params": {"path": "/etc/passwd"},
                }
            )

    peer.handle = handle  # type: ignore[method-assign]
    install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    refusal = next(message for message in peer.messages if message.get("id") == 8001)
    assert refusal["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_initialize_declares_no_client_capabilities(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    params = peer.sent("initialize")["params"]
    assert params["clientCapabilities"] == {}
    assert params["protocolVersion"] == 1
    assert params["clientInfo"]["name"] == "guildbotics"
    # Required by ACP; Grok rejects initialize without it.
    assert params["clientInfo"]["version"] == CLIENT_VERSION


@pytest.mark.asyncio
async def test_every_turn_runs_the_workspace_sandbox(monkeypatch, tmp_path) -> None:
    peer = _Peer()
    launched = install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path, read_only=True)

    assert launched[0][0][2:4] == ("--sandbox", "workspace")


@pytest.mark.asyncio
async def test_write_credentials_are_not_inherited(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    # Set, not deleted: the parent of a workflow run really does carry these,
    # so asserting their absence only proves isolation when they start present.
    for key in _AMBIENT_EXECUTION_ENV:
        monkeypatch.setenv(key, "stale-parent-value")
    peer = _Peer()
    launched = install(monkeypatch, peer)

    await _run(GrokAcpAdapter(), tmp_path)

    env = launched[0][1]["env"]
    assert "GH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert all(key not in env for key in _AMBIENT_EXECUTION_ENV)
    assert launched[0][1]["start_new_session"] is True


@pytest.mark.asyncio
async def test_cancellation_terminates_the_process_group(monkeypatch, tmp_path) -> None:
    peer = _Peer()
    install(monkeypatch, peer)
    adapter = GrokAcpAdapter()
    terminated: list[Any] = []

    async def terminate(process: Any) -> None:
        terminated.append(process)
        process.returncode = -15

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.acp.terminate_process_tree",
        terminate,
    )
    adapter._transport._process = peer  # type: ignore[assignment]
    adapter._active_session_id = _Peer.SESSION_ID

    await adapter.interrupt()

    assert terminated == [peer]
    assert peer.sent("session/cancel")["params"]["sessionId"] == _Peer.SESSION_ID


# --- pure helpers ------------------------------------------------------------


def test_launch_argv_places_options_in_their_parser_scopes() -> None:
    argv = _launch_argv("grok")

    assert argv.index("--no-auto-update") < argv.index("agent")
    assert argv.index("--sandbox") < argv.index("agent")
    assert argv.index("agent") < argv.index("--always-approve") < argv.index("stdio")


# --- observed 0.2.114 behaviour ---------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_chunks_never_reach_the_reply(monkeypatch, tmp_path) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "The user wants a short answer."},
            },
            text_chunk("OK"),
        ]
    )
    install(monkeypatch, peer)

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
        updates=[text_chunk("OK")],
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
    install(monkeypatch, peer)

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
    peer = _Peer(updates=[text_chunk("OK")], turn_usage={})
    install(monkeypatch, peer)

    result, events = await _run(GrokAcpAdapter(), tmp_path)

    assert result.usage == {}
    assert not [event for event in events if event.kind is AgentEventKind.USAGE]


@pytest.mark.asyncio
async def test_replayed_extension_usage_is_history_not_this_turn(
    monkeypatch, tmp_path
) -> None:
    """session/load replays the previous turn's usage on the extension channel."""
    peer = _Peer(
        replay=[text_chunk("the previous answer")],
        replay_extensions=[
            {
                "sessionUpdate": "turn_completed",
                "stop_reason": "end_turn",
                "usage": {"inputTokens": 999_999, "outputTokens": 111},
            }
        ],
        updates=[text_chunk("fresh")],
        turn_usage={"inputTokens": 12_641, "outputTokens": 19},
    )
    install(monkeypatch, peer)
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
        updates=[text_chunk("done")],
    )
    install(monkeypatch, peer)

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
        updates=[text_chunk("done")],
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)

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
            text_chunk("done"),
        ]
    )
    install(monkeypatch, peer)
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
    peer = _Peer(updates=[text_chunk("finally done")], prompt_delay=0.2)
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)
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
    peer = _Peer(updates=[text_chunk("too late")], prompt_delay=5.0)
    install(monkeypatch, peer)
    terminated: list[Any] = []

    async def terminate(process: Any) -> None:
        terminated.append(process)
        process.returncode = -15

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.acp.terminate_process_tree",
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
            text_chunk("Hel"),
            text_chunk("lo"),
        ]
    )
    install(monkeypatch, peer)

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
    peer = _Peer(stop_reason="max_tokens", updates=[text_chunk("partial")])
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)
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
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(GrokAcpAdapter(), tmp_path)

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
