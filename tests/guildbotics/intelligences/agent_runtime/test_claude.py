from __future__ import annotations

import asyncio
import json
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


def _context(tmp_path: Path) -> AgentExecutionContext:
    key = ConversationKey("aiko", "claude", "chat", "slack:U:C1:100.1")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
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
            assert kwargs["env"]["GUILDBOTICS_DATA_DIR"] == str(tmp_path)
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
    assert policy_event.details == {"bash_sandbox": False}
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
