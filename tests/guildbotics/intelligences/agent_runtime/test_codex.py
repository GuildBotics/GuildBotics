from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime.codex import (
    CodexAppServerAdapter,
    _agent_error_from_rpc,
    _decode_notification,
    _sandbox_policy,
    _thread_sandbox,
)
from guildbotics.intelligences.agent_runtime.environment import STREAM_READ_LIMIT
from guildbotics.intelligences.agent_runtime.jsonrpc import RpcError
from guildbotics.intelligences.agent_runtime.member_broker import (
    MEMBER_BROKER_TOKEN_ENV,
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
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)


class _Writer:
    def __init__(self, process: "_Process") -> None:
        self.process = process

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            self.process.handle(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.returncode = 0
        self.process.stdout.feed_eof()
        self.process.stderr.feed_eof()


class _Process:
    def __init__(
        self,
        *,
        malformed_turn: bool = False,
        complete_turn: bool = True,
        emit_assistant: bool = True,
        approval_method: str = "item/commandExecution/requestApproval",
        eof_turn: bool = False,
        oversized_turn: bool = False,
        stream_limit: int = 2**16,
    ) -> None:
        self.stdout = asyncio.StreamReader(limit=stream_limit)
        self.stderr = asyncio.StreamReader()
        self.stdin = _Writer(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.resume_thread = ""
        self.malformed_turn = malformed_turn
        self.complete_turn = complete_turn
        self.emit_assistant = emit_assistant
        self.approval_method = approval_method
        self.eof_turn = eof_turn
        self.oversized_turn = oversized_turn

    def handle(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if "method" not in message or "id" not in message:
            return
        method = message["method"]
        request_id = message["id"]
        if method == "initialize":
            self._result(request_id, {})
        elif method == "account/read":
            self._result(request_id, {"account": {"type": "chatgpt"}})
        elif method == "account/rateLimits/read":
            self._result(request_id, {"rate_limits": {}})
        elif method == "model/list":
            # The real envelope: a paginated `data` array whose entries carry
            # `supportedReasoningEfforts` as objects, not bare strings.
            self._result(
                request_id,
                {
                    "data": [
                        {
                            "id": "gpt-known",
                            "isDefault": False,
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "low", "description": "Fast"},
                                {"reasoningEffort": "high", "description": "Thorough"},
                            ],
                            "defaultReasoningEffort": "high",
                        },
                        {
                            "id": "gpt-default",
                            "isDefault": True,
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "low", "description": "Fast"}
                            ],
                            "defaultReasoningEffort": "low",
                        },
                    ],
                    "nextCursor": None,
                },
            )
        elif method == "thread/start":
            self._result(request_id, {"thread": {"id": "thread-1"}})
        elif method == "thread/resume":
            self.resume_thread = message["params"]["threadId"]
            self._result(request_id, {"thread": {"thread_id": self.resume_thread}})
        elif method == "turn/start":
            self._result(request_id, {"turn": {"id": "turn-1"}})
            if self.eof_turn:
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                return
            if self.malformed_turn:
                self.stdout.feed_data(b"not-json\n")
                return
            if not self.complete_turn:
                return
            if self.oversized_turn:
                self._feed(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "delta": "x" * (2 * 2**16),
                        },
                    }
                )
            if self.emit_assistant:
                self._feed(
                    {
                        "id": 999,
                        "method": self.approval_method,
                        "params": {"threadId": "thread-1", "turnId": "turn-1"},
                    }
                )
                self._feed(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "delta": "hello ",
                        },
                    }
                )
                self._feed(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "delta": "world",
                        },
                    }
                )
            self._feed(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 3,
                                "cachedInputTokens": 1,
                                "outputTokens": 2,
                                "reasoningOutputTokens": 1,
                                "totalTokens": 6,
                            },
                            "total": {},
                            "modelContextWindow": 200000,
                        },
                    },
                }
            )
            self._feed(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                        },
                    },
                }
            )

    def _result(self, request_id: int, result: Any) -> None:
        self._feed({"id": request_id, "result": result})

    def _feed(self, message: dict[str, Any]) -> None:
        encoded = (json.dumps(message) + "\n").encode()
        midpoint = max(1, len(encoded) // 2)
        self.stdout.feed_data(encoded[:midpoint])
        self.stdout.feed_data(encoded[midpoint:])

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
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


def _turn_start(process: "_Process") -> dict[str, Any]:
    return next(
        message["params"]
        for message in process.messages
        if message.get("method") == "turn/start"
    )


@pytest.mark.asyncio
async def test_codex_app_server_protocol_resumes_exact_thread_and_streams(
    monkeypatch, tmp_path
) -> None:
    process = _Process()

    async def create_process(*args, **kwargs):
        assert args[:2] == ("codex", "app-server")
        assert all(args[index] == "-c" for index in range(2, len(args), 2))
        config = args[3::2]
        assert any(
            value.endswith('.url="http://127.0.0.1:43123/mcp"') for value in config
        )
        assert any(
            value.endswith('.bearer_token_env_var="GUILDBOTICS_MEMBER_BROKER_TOKEN"')
            for value in config
        )
        assert not any(".env_http_headers=" in value for value in config)
        assert any(value.endswith(".required=true") for value in config)
        assert any('enabled_tools=["guildbotics_member"]' in value for value in config)
        assert kwargs["start_new_session"] is True
        assert kwargs["env"][MEMBER_BROKER_TOKEN_ENV]
        for key in (
            RUN_ENV,
            TASK_RUN_ENV,
            "GUILDBOTICS_WORKSPACE_ROOT",
            LEASE_ID_ENV,
            DELEGATION_ID_ENV,
            LEASE_PERSON_ENV,
            LEASE_RUN_ENV,
        ):
            assert key not in kwargs["env"]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter(policy=AdapterFilesystemPolicy())
    conversation = ConversationRecord(
        key=_context(tmp_path).conversation_key,
        provider_session_id="thread-1",
    )
    events: list[AgentEvent] = []

    result = await adapter.run_turn(
        "continue", _context(tmp_path), conversation, events.append
    )
    await adapter.close()

    assert result.output == "hello world"
    assert result.provider_session_id == "thread-1"
    assert result.provider_turn_id == "turn-1"
    assert result.usage == {
        "input_tokens": 3,
        "output_tokens": 2,
        "cached_input_tokens": 1,
        "reasoning_output_tokens": 1,
        "total_tokens": 6,
    }
    assert process.resume_thread == "thread-1"
    methods = [message.get("method") for message in process.messages]
    assert methods[:5] == [
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
        "thread/resume",
    ]
    request_ids = [
        message["id"]
        for message in process.messages
        if "method" in message and "id" in message
    ]
    assert len(request_ids) == len(set(request_ids))
    assert any(
        event.name == "decision" and event.approval == "decline" for event in events
    )


@pytest.mark.asyncio
async def test_codex_provider_never_inherits_the_parent_execution_grant(
    monkeypatch, tmp_path
) -> None:
    for key in (LEASE_ID_ENV, DELEGATION_ID_ENV, LEASE_PERSON_ENV, LEASE_RUN_ENV):
        monkeypatch.setenv(key, "stale-parent-value")
    process = _Process()
    launched: list[dict[str, Any]] = []

    async def create_process(*_args, **kwargs):
        launched.append(kwargs["env"])
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter(policy=AdapterFilesystemPolicy())
    context = _context(tmp_path)
    events: list[AgentEvent] = []
    await adapter.run_turn(
        "continue",
        context,
        ConversationRecord(key=context.conversation_key),
        events.append,
    )
    await adapter.close()

    env = launched[0]
    assert env[MEMBER_BROKER_TOKEN_ENV]
    for key in (LEASE_ID_ENV, DELEGATION_ID_ENV, LEASE_PERSON_ENV, LEASE_RUN_ENV):
        assert key not in env


@pytest.mark.asyncio
async def test_codex_reuses_process_for_multiple_exact_turns(
    monkeypatch, tmp_path
) -> None:
    process = _Process()
    starts = 0

    async def create_process(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()
    conversation = ConversationRecord(key=_context(tmp_path).conversation_key)

    first = await adapter.run_turn(
        "first", _context(tmp_path), conversation, lambda _event: None
    )
    conversation.provider_session_id = first.provider_session_id
    second = await adapter.run_turn(
        "second", _context(tmp_path), conversation, lambda _event: None
    )
    await adapter.close()

    assert starts == 1
    assert first.output == second.output == "hello world"
    assert process.resume_thread == "thread-1"
    thread_start = next(
        message
        for message in process.messages
        if message.get("method") == "thread/start"
    )
    assert thread_start["params"]["approvalPolicy"] == "never"
    assert thread_start["params"]["sandbox"] == "workspace-write"
    turn_start = next(
        message for message in process.messages if message.get("method") == "turn/start"
    )
    assert turn_start["params"]["approvalPolicy"] == "never"
    assert turn_start["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path)],
        "networkAccess": True,
    }


@pytest.mark.asyncio
async def test_codex_declines_legacy_approval_requests(monkeypatch, tmp_path) -> None:
    process = _Process(approval_method="execCommandApproval")

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    events: list[AgentEvent] = []
    await adapter.run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        events.append,
    )
    await adapter.close()

    assert {"id": 999, "result": {"decision": "denied"}} in process.messages
    assert any(event.approval == "decline" for event in events)


@pytest.mark.parametrize(
    ("filesystem_access", "expected"),
    [
        (
            "workspace",
            {
                "type": "workspaceWrite",
                "writableRoots": ["/workspace-data"],
                "networkAccess": True,
            },
        ),
        ("host", {"type": "dangerFullAccess"}),
    ],
)
def test_codex_turn_sandbox_policy_matches_filesystem_access(
    filesystem_access: str, expected: dict[str, Any]
) -> None:
    policy = AdapterFilesystemPolicy(filesystem_access=filesystem_access)

    assert _sandbox_policy(policy, "/workspace-data") == expected


@pytest.mark.parametrize("filesystem_access", ["workspace", "host"])
def test_codex_read_only_turn_overrides_the_configured_filesystem_access(
    filesystem_access: str,
) -> None:
    policy = AdapterFilesystemPolicy(filesystem_access=filesystem_access)

    # A read-only turn reads untrusted material, so it must not inherit the
    # member's configured write or network access.
    assert _sandbox_policy(policy, "/workspace-data", True) == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert _thread_sandbox(policy, True) == "read-only"


@pytest.mark.parametrize(
    ("filesystem_access", "expected"),
    [("workspace", "workspace-write"), ("host", "danger-full-access")],
)
def test_codex_thread_sandbox_matches_filesystem_access(
    filesystem_access: str, expected: str
) -> None:
    assert (
        _thread_sandbox(AdapterFilesystemPolicy(filesystem_access=filesystem_access))
        == expected
    )


@pytest.mark.asyncio
async def test_codex_rejects_permission_profile_request_with_json_rpc_error(
    monkeypatch, tmp_path
) -> None:
    process = _Process(approval_method="item/permissions/requestApproval")

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()
    events: list[AgentEvent] = []

    await adapter.run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        events.append,
    )
    await adapter.close()

    response = next(message for message in process.messages if message.get("id") == 999)
    assert response["error"]["code"] == -32601
    assert any(
        event.approval == "decline" and event.details["unsupported"] is True
        for event in events
    )


@pytest.mark.asyncio
async def test_codex_malformed_stream_is_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    process = _Process(malformed_turn=True)

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    await adapter.close()


@pytest.mark.asyncio
async def test_codex_restarts_process_after_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    processes = [_Process(malformed_turn=True), _Process()]

    async def create_process(*args, **kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()
    conversation = ConversationRecord(key=_context(tmp_path).conversation_key)

    with pytest.raises(AgentRuntimeError):
        await adapter.run_turn(
            "first", _context(tmp_path), conversation, lambda _event: None
        )

    recovered = await adapter.run_turn(
        "second", _context(tmp_path), conversation, lambda _event: None
    )
    await adapter.close()

    assert recovered.output == "hello world"
    assert recovered.provider_session_id == "thread-1"
    assert processes == []


@pytest.mark.asyncio
async def test_codex_stdout_eof_is_process_failure(monkeypatch, tmp_path) -> None:
    process = _Process(eof_turn=True)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert excinfo.value.rotate_session is True
    await adapter.close()


@pytest.mark.asyncio
async def test_codex_requests_stream_limit_for_oversized_lines(
    monkeypatch, tmp_path
) -> None:
    process = _Process(oversized_turn=True, stream_limit=STREAM_READ_LIMIT)
    limits: list[Any] = []

    async def create_process(*_args, **kwargs):
        limits.append(kwargs.get("limit"))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    result = await adapter.run_turn(
        "hello",
        _context(tmp_path),
        ConversationRecord(key=_context(tmp_path).conversation_key),
        lambda _event: None,
    )
    await adapter.close()

    assert result.output.endswith("hello world")
    assert len(result.output) > 2**17
    assert limits == [STREAM_READ_LIMIT]


@pytest.mark.asyncio
async def test_codex_oversized_line_beyond_limit_is_protocol_error(
    monkeypatch, tmp_path
) -> None:
    process = _Process(oversized_turn=True)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )
    await adapter.close()

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    assert "longer than limit" in str(excinfo.value)


def test_codex_compaction_notifications_are_provider_neutral() -> None:
    current = _decode_notification(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"id": "compact-1", "type": "contextCompaction"},
        },
    )
    legacy = _decode_notification(
        "thread/compacted", {"threadId": "thread-1", "turnId": "turn-1"}
    )

    assert current is not None and legacy is not None
    assert current.kind is AgentEventKind.TURN
    assert current.name == legacy.name == "context_compaction"
    assert current.item_id == "compact-1"


def test_codex_command_and_file_items_are_normalized() -> None:
    command = _decode_notification(
        "item/completed",
        {
            "item": {
                "id": "command-1",
                "type": "commandExecution",
                "command": ["uv", "run", "pytest"],
                "aggregatedOutput": "passed",
                "status": "completed",
            }
        },
    )
    changed = _decode_notification(
        "item/completed",
        {
            "item": {
                "id": "change-1",
                "type": "fileChange",
                "changes": [
                    {"path": "guildbotics/a.py", "kind": "update"},
                    {"path": "tests/test_a.py", "kind": "add"},
                ],
                "status": "completed",
            }
        },
    )

    assert command is not None and changed is not None
    assert command.kind is AgentEventKind.COMMAND
    assert command.command == "uv run pytest"
    assert command.message == "passed"
    assert changed.kind is AgentEventKind.FILE_CHANGE
    assert changed.path == "guildbotics/a.py"
    assert changed.details["paths"] == ["guildbotics/a.py", "tests/test_a.py"]
    assert _decode_notification("future/event", {"value": 1}) is None


def test_codex_empty_item_shells_are_skipped() -> None:
    for method in ("item/started", "item/completed"):
        assert (
            _decode_notification(
                method, {"item": {"id": "reason-1", "type": "reasoning"}}
            )
            is None
        )
    assert (
        _decode_notification(
            "item/started", {"item": {"id": "user-1", "type": "userMessage"}}
        )
        is None
    )
    completed = _decode_notification(
        "item/completed",
        {"item": {"id": "user-1", "type": "userMessage", "text": "injected context"}},
    )
    assert completed is not None
    assert completed.message == "injected context"


def test_codex_token_usage_notification_is_normalized() -> None:
    event = _decode_notification(
        "thread/tokenUsage/updated",
        {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "token_usage": {
                "last": {
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "total_tokens": 7,
                },
                "model_context_window": 200000,
            },
        },
    )

    assert event is not None
    assert event.kind is AgentEventKind.USAGE
    assert event.usage == {
        "input_tokens": 4,
        "output_tokens": 3,
        "total_tokens": 7,
    }
    assert event.details["model_context_window"] == 200000


@pytest.mark.asyncio
async def test_codex_terminal_error_notification_uses_structured_category(
    monkeypatch, tmp_path
) -> None:
    process = _Process(complete_turn=False)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    task = asyncio.create_task(
        adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )
    )
    while not adapter._active_turn_id:
        await asyncio.sleep(0)
    process._feed(
        {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
                "error": {
                    "message": "login expired",
                    "codexErrorInfo": "unauthorized",
                },
            },
        }
    )

    with pytest.raises(AgentRuntimeError) as excinfo:
        await task

    assert excinfo.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert excinfo.value.rotate_session is True
    await adapter.close()


@pytest.mark.asyncio
async def test_codex_timeout_interrupts_and_rotates_session(
    monkeypatch, tmp_path
) -> None:
    process = _Process(complete_turn=False)

    async def create_process(*_args, **_kwargs):
        return process

    interrupted = False
    adapter = CodexAppServerAdapter(timeout=0.01)

    async def interrupt() -> None:
        nonlocal interrupted
        interrupted = True

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
    await adapter.close()


@pytest.mark.asyncio
async def test_codex_interrupt_still_terminates_after_rpc_is_cancelled(
    monkeypatch,
) -> None:
    process = _Process(complete_turn=False)
    adapter = CodexAppServerAdapter()
    adapter._transport._process = process
    adapter._active_thread_id = "thread-1"
    adapter._active_turn_id = "turn-1"
    terminated = False

    async def cancelled_request(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def terminate(_process) -> None:
        nonlocal terminated
        terminated = True
        process.returncode = -15

    monkeypatch.setattr(adapter, "_request", cancelled_request)
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.codex.terminate_process_tree",
        terminate,
    )

    await adapter.interrupt()

    assert terminated is True


@pytest.mark.asyncio
async def test_codex_empty_terminal_response_is_protocol_failure(
    monkeypatch, tmp_path
) -> None:
    process = _Process(emit_assistant=False)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await adapter.run_turn(
            "hello",
            _context(tmp_path),
            ConversationRecord(key=_context(tmp_path).conversation_key),
            lambda _event: None,
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    await adapter.close()


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            {"data": {"type": "authentication_failed"}},
            AgentRuntimeErrorCategory.AUTHENTICATION,
        ),
        (
            {"data": {"category": "rate_limited", "retryAfterSeconds": 12}},
            AgentRuntimeErrorCategory.RATE_LIMITED,
        ),
        ({"code": -32601}, AgentRuntimeErrorCategory.UNSUPPORTED_VERSION),
        ({"code": 500}, AgentRuntimeErrorCategory.PROTOCOL),
    ],
)
def test_codex_rpc_errors_use_structured_categories(error, category) -> None:
    normalized = _agent_error_from_rpc(RpcError(error))
    assert normalized.category is category


@pytest.mark.asyncio
async def test_codex_account_and_rate_limit_accept_snake_case_schema(
    monkeypatch,
) -> None:
    adapter = CodexAppServerAdapter()

    async def auth_request(method, _params):
        assert method == "account/read"
        return {"requires_openai_auth": True, "account": None}

    monkeypatch.setattr(adapter, "_request", auth_request)
    with pytest.raises(AgentRuntimeError) as auth_error:
        await adapter._check_account()
    assert auth_error.value.category is AgentRuntimeErrorCategory.AUTHENTICATION

    async def rate_request(method, _params):
        assert method == "account/rateLimits/read"
        return {
            "rate_limits": {
                "primary": {"used_percent": 100, "resets_at": 2_000_000_000}
            }
        }

    monkeypatch.setattr(adapter, "_request", rate_request)
    with pytest.raises(AgentRuntimeError) as rate_error:
        await adapter._check_rate_limits()
    assert rate_error.value.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert rate_error.value.details["retry_after_at"]


# --------------------------------------------------------------------------- #
# Effort: turn/start settings and model/list validation
# --------------------------------------------------------------------------- #


async def _run_turn_returning(
    monkeypatch, tmp_path, conversation_overrides=None, **context_overrides
):
    """Run one turn and return the fake process together with its result."""
    process = _Process()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = CodexAppServerAdapter(policy=AdapterFilesystemPolicy())
    context = _context(tmp_path, **context_overrides)
    conversation = ConversationRecord(
        key=context.conversation_key,
        provider_session_id="thread-1",
        **(conversation_overrides or {}),
    )
    terminal = await adapter.run_turn("go", context, conversation, lambda _event: None)
    await adapter.close()
    return process, terminal


async def _run_turn_with(monkeypatch, tmp_path, **context_overrides) -> "_Process":
    process, _ = await _run_turn_returning(monkeypatch, tmp_path, **context_overrides)
    return process


def test_codex_applies_settings_per_turn_so_a_change_needs_no_new_thread() -> None:
    assert CodexAppServerAdapter.settings_scope == "turn"


@pytest.mark.asyncio
async def test_codex_turn_start_carries_effort_and_model(monkeypatch, tmp_path) -> None:
    process = await _run_turn_with(
        monkeypatch,
        tmp_path,
        effort="high",
        provider_options={"model": "gpt-known", "effort": "high"},
    )
    params = _turn_start(process)
    assert params["model"] == "gpt-known"
    assert params["effort"] == "high"


@pytest.mark.asyncio
async def test_codex_turn_start_omits_settings_when_nothing_is_imposed(
    monkeypatch, tmp_path
) -> None:
    """Omitting them is how "keep the thread's current settings" is expressed."""
    process = await _run_turn_with(monkeypatch, tmp_path)
    params = _turn_start(process)
    assert "model" not in params
    assert "effort" not in params


@pytest.mark.asyncio
async def test_codex_does_not_fall_back_to_the_label_when_unmapped(
    monkeypatch, tmp_path
) -> None:
    """An unmapped level is "no intervention" here too, as diagnostics records.

    Using the provider-neutral label as a fallback would make Codex intervene on
    exactly the runs recorded as `unsupported`, and would send the label
    `default` -- which is not a Codex effort value -- as if it were one.
    """
    process = await _run_turn_with(
        monkeypatch, tmp_path, effort="high", provider_options={}
    )
    params = _turn_start(process)
    assert "effort" not in params
    assert "model" not in params


@pytest.mark.asyncio
async def test_codex_drops_an_effort_the_model_does_not_support(
    monkeypatch, tmp_path, caplog
) -> None:
    process = await _run_turn_with(
        monkeypatch,
        tmp_path,
        effort="high",
        provider_options={"model": "gpt-known", "effort": "extra-high"},
    )
    params = _turn_start(process)
    assert params["model"] == "gpt-known"
    assert "effort" not in params


@pytest.mark.asyncio
async def test_codex_drops_a_model_the_installation_does_not_offer(
    monkeypatch, tmp_path
) -> None:
    process = await _run_turn_with(
        monkeypatch,
        tmp_path,
        effort="high",
        # `gpt-default` is the installation's default and supports only `low`,
        # so once the unknown model is dropped the effort is judged against it.
        provider_options={"model": "gpt-unavailable", "effort": "low"},
    )
    params = _turn_start(process)
    assert "model" not in params
    assert params["effort"] == "low"


@pytest.mark.asyncio
async def test_codex_validates_effort_against_the_default_model_when_none_is_named(
    monkeypatch, tmp_path
) -> None:
    """A mapping with no `model` still runs on a model: the thread's default.

    Skipping validation here would send an effort the default model does not
    accept, which is exactly the case a mapping without a model produces.
    """
    process = await _run_turn_with(
        monkeypatch, tmp_path, effort="high", provider_options={"effort": "high"}
    )
    params = _turn_start(process)
    assert "effort" not in params

    supported = await _run_turn_with(
        monkeypatch, tmp_path, effort="low", provider_options={"effort": "low"}
    )
    assert _turn_start(supported)["effort"] == "low"


@pytest.mark.asyncio
async def test_codex_terminal_result_carries_the_settings_the_turn_ran_with(
    monkeypatch, tmp_path
) -> None:
    _, terminal = await _run_turn_returning(
        monkeypatch,
        tmp_path,
        effort="high",
        provider_options={"model": "gpt-known", "effort": "high"},
    )
    assert (terminal.model, terminal.effort) == ("gpt-known", "high")


@pytest.mark.asyncio
async def test_codex_terminal_result_names_the_catalog_defaults_when_none_imposed(
    monkeypatch, tmp_path
) -> None:
    """A thread that never had settings imposed runs on the catalog defaults:
    the default model entry and that model's ``defaultReasoningEffort``."""
    _, terminal = await _run_turn_returning(monkeypatch, tmp_path)
    assert terminal.model == "gpt-default"
    assert terminal.effort == "low"


@pytest.mark.asyncio
async def test_a_resumed_thread_keeps_the_settings_the_last_turn_imposed(
    monkeypatch, tmp_path
) -> None:
    """Omitting a setting keeps whatever the last ``turn/start`` carried, so
    the effective values come from the conversation, not the catalog default."""
    _, terminal = await _run_turn_returning(
        monkeypatch,
        tmp_path,
        conversation_overrides={
            "effective_model": "gpt-earlier",
            "effective_effort": "high",
        },
    )
    assert (terminal.model, terminal.effort) == ("gpt-earlier", "high")


@pytest.mark.asyncio
async def test_an_effort_the_conversation_imposed_beats_the_models_default(
    monkeypatch, tmp_path
) -> None:
    """The catalog default only describes a thread nothing was imposed on."""
    _, terminal = await _run_turn_returning(
        monkeypatch,
        tmp_path,
        provider_options={"model": "gpt-known"},
        conversation_overrides={"effective_effort": "low"},
    )
    assert (terminal.model, terminal.effort) == ("gpt-known", "low")


@pytest.mark.asyncio
async def test_a_dropped_effort_falls_back_to_the_models_advertised_default(
    monkeypatch, tmp_path
) -> None:
    """An unsupported effort never reaches ``turn/start``, and a conversation
    that never imposed one runs at the model's own advertised default."""
    _, terminal = await _run_turn_returning(
        monkeypatch,
        tmp_path,
        effort="high",
        provider_options={"model": "gpt-known", "effort": "extra-high"},
    )
    assert terminal.model == "gpt-known"
    assert terminal.effort == "high"


@pytest.mark.asyncio
async def test_codex_reports_unsupported_effort_settings_instead_of_dropping_them(
    monkeypatch, tmp_path, caplog
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        process = await _run_turn_with(
            monkeypatch,
            tmp_path,
            effort="high",
            provider_options={"effort": "high", "temperature": 0.9},
        )
    params = _turn_start(process)
    assert "temperature" not in params
    assert "temperature" in caplog.text
