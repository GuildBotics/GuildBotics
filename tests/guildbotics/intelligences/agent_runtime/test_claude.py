from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from guildbotics.capabilities.task_runs import RUN_ENV
from guildbotics.intelligences.agent_runtime.claude import (
    ClaudeStreamJsonAdapter,
    _decode_events,
    _session_limit_error,
)
from guildbotics.intelligences.agent_runtime.environment import STREAM_READ_LIMIT
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
)


class _Input:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _HelpProcess:
    returncode = 0

    async def communicate(self):
        return (
            b"--input-format stream-json --output-format stream-json --resume",
            b"",
        )


class _StreamProcess:
    def __init__(
        self,
        messages: list[Any],
        *,
        returncode: int | None = 0,
        stderr: bytes = b"",
        stream_limit: int = 2**16,
    ) -> None:
        self.stdin = _Input()
        self.stdout = asyncio.StreamReader(limit=stream_limit)
        self.stderr = asyncio.StreamReader()
        self.returncode = returncode
        for message in messages:
            line = (
                message if isinstance(message, bytes) else json.dumps(message).encode()
            )
            encoded = line + b"\n"
            midpoint = max(1, len(encoded) // 2)
            self.stdout.feed_data(encoded[:midpoint])
            self.stdout.feed_data(encoded[midpoint:])
        self.stdout.feed_eof()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "claude", "chat", "slack:U:C1:100.1")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
        **overrides,
    )


@pytest.mark.asyncio
async def test_claude_stream_json_resumes_exact_session_and_emits_tool_lifecycle(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "stream_event",
                "session_id": "session-1",
                "event": {"delta": {"type": "text_delta", "text": "working"}},
            },
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "uv run pytest"},
                        },
                        {
                            "type": "tool_use",
                            "id": "write-1",
                            "name": "Write",
                            "input": {"file_path": "guildbotics/a.py"},
                        },
                        {"type": "text", "text": "done"},
                    ]
                },
            },
            {
                "type": "user",
                "session_id": "session-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "ok",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "write-1",
                            "content": "written",
                        },
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "final",
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
        ]
    )
    calls: list[tuple[Any, ...]] = []

    async def create_process(*args, **kwargs):
        calls.append(args)
        if args[-1] != "--help":
            assert kwargs["env"][RUN_ENV] == "run-1"
            assert kwargs["env"]["GUILDBOTICS_WORKSPACE_ROOT"] == str(tmp_path)
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()
    events: list[AgentEvent] = []

    result = await adapter.run_turn(
        "continue",
        _context(tmp_path),
        ConversationRecord(
            key=_context(tmp_path).conversation_key,
            provider_session_id="session-1",
        ),
        events.append,
    )

    assert result.output == "final"
    assert result.provider_session_id == "session-1"
    assert result.usage == {"input_tokens": 4, "output_tokens": 3}
    run_args = calls[1]
    assert run_args[run_args.index("--resume") + 1] == "session-1"
    assert "--continue" not in run_args
    assert run_args[run_args.index("--permission-mode") + 1] == "bypassPermissions"
    assert json.loads(run_args[run_args.index("--settings") + 1]) == {
        "sandbox": {"enabled": False}
    }
    policy_event = next(event for event in events if event.name == "policy")
    assert policy_event.approval == "bypassPermissions"
    assert policy_event.details == {"bash_sandbox": False, "read_only": False}
    assert "--allowed-tools" not in run_args
    command_events = [event for event in events if event.kind is AgentEventKind.COMMAND]
    assert [(event.name, event.item_id) for event in command_events] == [
        ("started", "tool-1"),
        ("completed", "tool-1"),
    ]
    assert all(event.command == "uv run pytest" for event in command_events)
    file_events = [
        event for event in events if event.kind is AgentEventKind.FILE_CHANGE
    ]
    assert [(event.name, event.item_id) for event in file_events] == [
        ("started", "write-1"),
        ("completed", "write-1"),
    ]
    assert all(event.path == "guildbotics/a.py" for event in file_events)
    sent = json.loads(bytes(stream.stdin.data))
    assert sent["message"]["content"][0]["text"] == "continue"


def _oversized_replay_messages() -> list[Any]:
    return [
        {"type": "system", "subtype": "init", "session_id": "session-1"},
        {
            "type": "user",
            "session_id": "session-1",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "x" * (2 * 2**16),
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "session-1",
            "result": "final",
            "usage": {},
        },
    ]


@pytest.mark.asyncio
async def test_claude_requests_stream_limit_for_oversized_lines(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        _oversized_replay_messages(), stream_limit=STREAM_READ_LIMIT
    )
    limits: list[Any] = []

    async def create_process(*args, **kwargs):
        if args[-1] == "--help":
            return _HelpProcess()
        limits.append(kwargs.get("limit"))
        return stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()

    result = await adapter.run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        lambda _event: None,
    )

    assert result.output == "final"
    assert limits == [STREAM_READ_LIMIT]


@pytest.mark.asyncio
async def test_claude_oversized_line_beyond_limit_is_protocol_error(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(_oversized_replay_messages())

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    assert "longer than limit" in str(excinfo.value)


def test_claude_compaction_event_is_provider_neutral() -> None:
    events = _decode_events(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "auto", "pre_tokens": 150_000},
        },
        "session-1",
    )

    assert len(events) == 1
    assert events[0].kind is AgentEventKind.TURN
    assert events[0].name == "context_compaction"
    assert events[0].details["compact_metadata"]["trigger"] == "auto"


def test_claude_rate_limit_event_diagnostics_are_selective() -> None:
    # The allowed event that opens every turn is noise unless it carries a
    # utilization reading; any non-allowed status is always recorded.
    allowed = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "resetsAt": 1_786_155_000,
            "rateLimitType": "five_hour",
        },
    }
    assert _decode_events(allowed, "session-1") == []

    with_utilization = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "resetsAt": 1_786_155_000,
            "rateLimitType": "five_hour",
            "utilization": 56,
        },
    }
    events = _decode_events(with_utilization, "session-1")
    assert len(events) == 1
    assert events[0].kind is AgentEventKind.TURN
    assert events[0].name == "rate_limit_status"
    assert events[0].details["utilization"] == 56
    assert events[0].details["rate_limit_type"] == "five_hour"
    assert events[0].details["retry_after_at"] == "2026-08-08T02:10:00+00:00"

    rejected = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day"},
    }
    events = _decode_events(rejected, "session-1")
    assert len(events) == 1
    assert events[0].details["status"] == "rejected"
    assert "retry_after_at" not in events[0].details

    assert _decode_events({"type": "rate_limit_event"}, "session-1") == []


def test_claude_file_tool_is_normalized() -> None:
    events = _decode_events(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "write-1",
                        "name": "Write",
                        "input": {"file_path": "guildbotics/a.py"},
                    }
                ]
            },
        },
        "session-1",
    )

    assert len(events) == 1
    assert events[0].kind is AgentEventKind.FILE_CHANGE
    assert events[0].path == "guildbotics/a.py"
    assert events[0].command == "Write"
    assert _decode_events({"type": "future_event"}, "session-1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "category"),
    [
        ([b"not-json"], AgentRuntimeErrorCategory.PROTOCOL),
        ([], AgentRuntimeErrorCategory.PROTOCOL),
        (
            [
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "error": "authentication_failed",
                }
            ],
            AgentRuntimeErrorCategory.AUTHENTICATION,
        ),
    ],
)
async def test_claude_structured_failures_rotate_when_required(
    monkeypatch, tmp_path, messages, category
) -> None:
    stream = _StreamProcess(messages)

    async def create_process(*args, **kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is category
    if category is AgentRuntimeErrorCategory.AUTHENTICATION:
        assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_claude_rate_limit_retry_can_recover_in_same_session(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {
                "type": "system",
                "subtype": "api_retry",
                "error": "rate_limit",
                "attempt": 1,
                "max_retries": 3,
                "retry_delay_ms": 2500,
            },
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "recovered",
            },
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    result = await ClaudeStreamJsonAdapter().run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        lambda _event: None,
    )

    assert result.output == "recovered"
    assert result.provider_session_id == "session-1"


@pytest.mark.asyncio
async def test_claude_terminal_error_after_rate_limit_preserves_session(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {
                "type": "system",
                "subtype": "api_retry",
                "error": "rate_limit",
                "attempt": 1,
                "max_retries": 1,
                "retry_delay_ms": 2500,
            },
            {
                "type": "result",
                "subtype": "error_during_execution",
                "session_id": "session-1",
                "result": "rate limited",
                "is_error": True,
            },
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert excinfo.value.details["retry_after_seconds"] == 2.5
    assert excinfo.value.rotate_session is False


@pytest.mark.asyncio
async def test_claude_rejected_rate_limit_event_names_the_exact_reset(
    monkeypatch, tmp_path
) -> None:
    # The rejected unified rate-limit event carries an epoch reset instant;
    # it must win over the api_retry error that only knows a retry delay,
    # regardless of arrival order.
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": 1_786_155_000,
                    "rateLimitType": "five_hour",
                },
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "api_retry",
                "error": "rate_limit",
                "attempt": 1,
                "max_retries": 1,
                "retry_delay_ms": 2500,
            },
            {
                "type": "result",
                "subtype": "error_during_execution",
                "session_id": "session-1",
                "result": "rate limited",
                "is_error": True,
            },
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert excinfo.value.details["retry_after_at"] == "2026-08-08T02:10:00+00:00"
    assert excinfo.value.details["rate_limit_type"] == "five_hour"
    assert excinfo.value.rotate_session is False


@pytest.mark.asyncio
async def test_claude_rejected_rate_limit_event_is_harmless_on_success(
    monkeypatch, tmp_path
) -> None:
    # A rejection the CLI recovers from (e.g. overage kicks in) must not fail
    # the turn; the stored error is only raised on a terminal error.
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 1_786_155_000},
                "session_id": "session-1",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "recovered",
            },
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    result = await ClaudeStreamJsonAdapter().run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        lambda _event: None,
    )

    assert result.output == "recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subtype", "message", "category", "details", "rotate_session"),
    [
        pytest.param(
            "success",
            "You've hit your session limit · resets 12:50pm (Asia/Tokyo)",
            AgentRuntimeErrorCategory.RATE_LIMITED,
            {
                "retry_after_text": "resets 12:50pm (Asia/Tokyo)",
                "retry_after_timezone": "Asia/Tokyo",
            },
            False,
            id="session-limit",
        ),
        pytest.param(
            "error_during_execution",
            "provider stopped unexpectedly",
            AgentRuntimeErrorCategory.PROCESS,
            {"subtype": "error_during_execution"},
            True,
            id="process-failure",
        ),
    ],
)
async def test_claude_terminal_result_error_classification(
    monkeypatch, tmp_path, subtype, message, category, details, rotate_session
) -> None:
    stream = _StreamProcess(
        [
            {
                "type": "result",
                "subtype": subtype,
                "session_id": "session-1",
                "result": message,
                "is_error": True,
            }
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is category
    assert excinfo.value.details == details
    assert excinfo.value.rotate_session is rotate_session


def test_claude_session_limit_without_reset_time_is_rate_limited() -> None:
    error = _session_limit_error("You've hit your session limit")

    assert error is not None
    assert error.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert error.details == {}


@pytest.mark.asyncio
async def test_claude_terminal_result_does_not_wait_for_inherited_pipes(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "done",
            },
        ]
    )
    stream.stdout = asyncio.StreamReader()
    for message in (
        {"type": "system", "subtype": "init", "session_id": "session-1"},
        {
            "type": "result",
            "subtype": "success",
            "session_id": "session-1",
            "result": "done",
        },
    ):
        stream.stdout.feed_data((json.dumps(message) + "\n").encode())
    stream.stderr = asyncio.StreamReader()

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    async def terminate(_process) -> None:
        stream.stdout.feed_eof()
        stream.stderr.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.claude.terminate_process_tree",
        terminate,
    )

    result = await asyncio.wait_for(
        ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        ),
        timeout=0.5,
    )

    assert result.output == "done"


@pytest.mark.asyncio
async def test_claude_success_result_survives_cleanup_sigterm(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "valid response",
            },
        ],
        returncode=None,
    )

    async def wait_forever() -> int:
        await asyncio.Future()
        return 0

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    async def terminate(_process) -> None:
        stream.returncode = -15

    stream.wait = wait_forever
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.claude._PROCESS_EXIT_GRACE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.claude.terminate_process_tree",
        terminate,
    )

    result = await ClaudeStreamJsonAdapter().run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        lambda _event: None,
    )

    assert stream.returncode == -15
    assert result.output == "valid response"
    assert result.provider_session_id == "session-1"
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_claude_rejects_versions_without_stream_json(
    monkeypatch, tmp_path
) -> None:
    class OldHelp(_HelpProcess):
        async def communicate(self):
            return b"--resume", b""

    async def create_process(*args, **kwargs):
        return OldHelp()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_claude_nonzero_exit_is_process_failure(monkeypatch, tmp_path) -> None:
    stream = _StreamProcess([], returncode=9, stderr=b"provider stopped\n")

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert excinfo.value.details["returncode"] == 9
    assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_claude_timeout_interrupts_and_rotates_session(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess([])
    stream.returncode = None
    stream.stdout = asyncio.StreamReader()
    stream.stderr = asyncio.StreamReader()

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    interrupted = False
    adapter = ClaudeStreamJsonAdapter(timeout=0.01)

    async def interrupt() -> None:
        nonlocal interrupted
        interrupted = True
        stream.returncode = -15
        stream.stdout.feed_eof()
        stream.stderr.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(adapter, "interrupt", interrupt)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert interrupted is True
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_claude_empty_terminal_response_is_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "",
            },
        ]
    )

    async def create_process(*args, **_kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await ClaudeStreamJsonAdapter().run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_claude_read_only_turn_is_confined_by_the_provider(
    monkeypatch, tmp_path
) -> None:
    stream = _StreamProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "session-9"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-9",
                "result": "answer",
                "usage": {},
            },
        ]
    )
    calls: list[tuple[Any, ...]] = []

    async def create_process(*args, **kwargs):
        calls.append(args)
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    context = replace(_context(tmp_path), read_only=True)
    events: list[AgentEvent] = []

    await adapter_run(monkeypatch, context, events)

    run_args = calls[1]
    # Diagnostics an assistant reads are untrusted input, so the limit has to be
    # the provider's, not the prompt's.
    assert run_args[run_args.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in run_args
    allowed = run_args[run_args.index("--allowed-tools") + 1 :]
    assert "Bash(guildbotics diagnostics:*)" in allowed
    assert "Read" in allowed
    disallowed = run_args[run_args.index("--disallowed-tools") + 1 :]
    for tool in ("Write", "Edit", "WebFetch", "WebSearch"):
        assert tool in disallowed
    policy_event = next(event for event in events if event.name == "policy")
    assert policy_event.details == {"bash_sandbox": False, "read_only": True}


async def adapter_run(monkeypatch, context, events) -> None:
    adapter = ClaudeStreamJsonAdapter()
    await adapter.run_turn(
        "why did it fail?",
        context,
        ConversationRecord(key=context.conversation_key),
        events.append,
    )


# --------------------------------------------------------------------------- #
# Effort: CLI flags, level validation, and the session-scoped settings contract
# --------------------------------------------------------------------------- #


async def _run_turn_with(monkeypatch, tmp_path, **context_overrides):
    """Run one turn and return the (args, env) Claude Code was launched with."""
    stream = _StreamProcess(
        [
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "final",
                "usage": {},
            }
        ]
    )
    launches: list[tuple[tuple[Any, ...], dict[str, str]]] = []

    async def create_process(*args, **kwargs):
        launches.append((args, dict(kwargs.get("env") or {})))
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()
    context = _context(tmp_path, **context_overrides)
    await adapter.run_turn(
        "go",
        context,
        ConversationRecord(key=context.conversation_key),
        lambda _event: None,
    )
    return launches[1]


async def _terminal_of(
    monkeypatch,
    tmp_path,
    *,
    init_model: str,
    conversation: ConversationRecord | None = None,
    **context_overrides,
):
    """Run one turn whose init event names ``init_model``, and return its result."""
    stream = _StreamProcess(
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": init_model,
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "session-1",
                "result": "final",
                "usage": {},
            },
        ]
    )

    async def create_process(*args, **kwargs):
        return _HelpProcess() if args[-1] == "--help" else stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = ClaudeStreamJsonAdapter()
    context = _context(tmp_path, **context_overrides)
    return await adapter.run_turn(
        "go",
        context,
        conversation or ConversationRecord(key=context.conversation_key),
        lambda _event: None,
    )


@pytest.mark.asyncio
async def test_claude_terminal_result_carries_the_model_the_session_reported(
    monkeypatch, tmp_path
) -> None:
    terminal = await _terminal_of(
        monkeypatch,
        tmp_path,
        init_model="claude-sonnet-5",
        effort="high",
        provider_options={"model": "opus-x", "effort": "xhigh"},
    )
    # The provider's own report wins over the requested model: they differ
    # exactly when the request was an alias or was overridden.
    assert terminal.model == "claude-sonnet-5"
    # Claude Code names no effort, so the honest value is the level the adapter
    # imposed, in Claude Code's own vocabulary rather than the neutral label.
    assert terminal.effort == "xhigh"


@pytest.mark.asyncio
async def test_claude_terminal_result_claims_no_effort_when_it_imposed_none(
    monkeypatch, tmp_path
) -> None:
    """A turn that imposes nothing leaves the session as it was, so it reports
    no effort of its own -- Claude Code never names one."""
    terminal = await _terminal_of(
        monkeypatch, tmp_path, init_model="claude-sonnet-5", effort="high"
    )
    assert terminal.model == "claude-sonnet-5"
    assert terminal.effort == ""


@pytest.mark.asyncio
async def test_a_continued_session_keeps_reporting_the_effort_it_runs_under(
    monkeypatch, tmp_path
) -> None:
    """Imposing nothing keeps the session's settings, so the effort the session
    was established with is still the effective one."""
    context_key_source = _context(tmp_path)
    conversation = ConversationRecord(
        key=context_key_source.conversation_key,
        provider_session_id="session-1",
        effective_effort="high",
    )
    terminal = await _terminal_of(
        monkeypatch,
        tmp_path,
        init_model="claude-sonnet-5",
        conversation=conversation,
    )
    assert terminal.effort == "high"


def test_claude_settings_are_session_scoped_so_a_change_rotates() -> None:
    assert ClaudeStreamJsonAdapter.settings_scope == "session"


@pytest.mark.asyncio
async def test_claude_effort_model_becomes_a_model_flag(monkeypatch, tmp_path) -> None:
    args, _ = await _run_turn_with(
        monkeypatch, tmp_path, effort="high", provider_options={"model": "opus-x"}
    )
    assert args[args.index("--model") + 1] == "opus-x"


@pytest.mark.asyncio
async def test_claude_effort_level_becomes_an_effort_flag(
    monkeypatch, tmp_path
) -> None:
    args, env = await _run_turn_with(
        monkeypatch,
        tmp_path,
        effort="high",
        provider_options={"effort": "high"},
    )
    assert args[args.index("--effort") + 1] == "high"
    # The level travels on the command line only; the old thinking-budget
    # variable is gone.
    assert "MAX_THINKING_TOKENS" not in env


@pytest.mark.asyncio
async def test_claude_drops_an_effort_level_it_does_not_offer(
    monkeypatch, tmp_path, caplog
) -> None:
    """An unknown level is warned about and dropped, so the turn still runs."""
    import logging

    with caplog.at_level(logging.WARNING):
        args, _ = await _run_turn_with(
            monkeypatch,
            tmp_path,
            effort="high",
            provider_options={"effort": "extreme"},
        )
    assert "--effort" not in args
    assert "extreme" in caplog.text


@pytest.mark.asyncio
async def test_claude_imposes_nothing_when_no_effort_is_requested(
    monkeypatch, tmp_path
) -> None:
    args, env = await _run_turn_with(monkeypatch, tmp_path)
    assert "--model" not in args
    assert "--effort" not in args
    assert "MAX_THINKING_TOKENS" not in env


@pytest.mark.asyncio
async def test_claude_reports_no_effort_when_it_only_imposed_a_model(
    monkeypatch, tmp_path
) -> None:
    """A model-only mapping changes no effort, so none may be claimed."""
    terminal = await _terminal_of(
        monkeypatch,
        tmp_path,
        init_model="claude-sonnet-5",
        effort="high",
        provider_options={"model": "opus-x"},
    )
    assert terminal.effort == ""


@pytest.mark.asyncio
async def test_claude_reports_unsupported_effort_settings(
    monkeypatch, tmp_path, caplog
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        args, _ = await _run_turn_with(
            monkeypatch,
            tmp_path,
            effort="high",
            provider_options={"model": "opus-x", "reasoning_effort": "high"},
        )
    assert "--reasoning_effort" not in args
    assert "reasoning_effort" in caplog.text
