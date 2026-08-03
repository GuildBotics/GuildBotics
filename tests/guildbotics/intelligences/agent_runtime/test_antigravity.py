from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.capabilities.task_runs import RUN_ENV
from guildbotics.intelligences.agent_runtime.antigravity import (
    _MAX_PROMPT_BYTES,
    AntigravityStreamJsonAdapter,
    _decode_events,
    _result_error,
    _usage,
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
from guildbotics.intelligences.brains.cli_agent import normalize_cli_agent_retry_after

FIXTURE = (
    Path(__file__).parent / "fixtures" / "antigravity_stream_1_1_10.jsonl"
).read_text()

_HELP_TEXT = (
    b"--print --output-format stream-json --conversation "
    b"--model --effort --add-dir --dangerously-skip-permissions"
)
_MODELS_TEXT = b"gemini-3.6-flash-low\ngemini-3.6-flash-high\nclaude-sonnet-4-6\n"


class _CompletedProcess:
    """A short-lived probe subprocess (``--help`` / ``models``)."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _HangingProcess:
    """A turn that never writes a line and never exits."""

    def __init__(self) -> None:
        # Neither reader is ever fed or closed, so readline() blocks forever.
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.pid = 0

    async def wait(self) -> int:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _StreamProcess:
    def __init__(
        self,
        lines: list[Any],
        *,
        returncode: int | None = 0,
        stderr: bytes = b"",
        stream_limit: int = 2**16,
    ) -> None:
        self.stdout = asyncio.StreamReader(limit=stream_limit)
        self.stderr = asyncio.StreamReader()
        self.returncode = returncode
        self.pid = 0
        for message in lines:
            raw = (
                message
                if isinstance(message, bytes)
                else json.dumps(message).encode()
            )
            encoded = raw + b"\n"
            # Split each line so readline() has to stitch chunks back together.
            midpoint = max(1, len(encoded) // 2)
            self.stdout.feed_data(encoded[:midpoint])
            self.stdout.feed_data(encoded[midpoint:])
        self.stdout.feed_eof()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def _fixture_lines() -> list[bytes]:
    return [line.encode() for line in FIXTURE.splitlines() if line.strip()]


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "antigravity", "chat", "slack:U:C1:100.1")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
        **overrides,
    )


def _install(
    monkeypatch,
    stream: Any,
    *,
    models: bytes = _MODELS_TEXT,
    models_returncode: int = 0,
    calls: list[tuple[Any, ...]] | None = None,
    kwargs_log: list[dict[str, Any]] | None = None,
) -> None:
    async def create_process(*args, **kwargs):
        if calls is not None:
            calls.append(args)
        if kwargs_log is not None:
            kwargs_log.append(kwargs)
        if args[-1] == "--help":
            return _CompletedProcess(stdout=b"", stderr=_HELP_TEXT)
        if args[-1] == "models":
            return _CompletedProcess(stdout=models, returncode=models_returncode)
        return stream

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)


async def _run(
    adapter: AntigravityStreamJsonAdapter,
    context: AgentExecutionContext,
    events: list[AgentEvent],
    *,
    session_id: str = "",
    prompt: str = "go",
):
    return await adapter.run_turn(
        prompt,
        context,
        ConversationRecord(key=context.conversation_key, provider_session_id=session_id),
        events.append,
    )


@pytest.mark.asyncio
async def test_conversation_id_from_init_becomes_the_session_and_events_map(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[Any, ...]] = []
    kwargs_log: list[dict[str, Any]] = []
    _install(
        monkeypatch,
        _StreamProcess(_fixture_lines()),
        calls=calls,
        kwargs_log=kwargs_log,
    )
    adapter = AntigravityStreamJsonAdapter()
    events: list[AgentEvent] = []

    result = await _run(adapter, _context(tmp_path), events)

    assert result.output == "Reading the file and wrote the result."
    assert result.provider_session_id == "8607305b-c0a7-4707-86fe-06e8314502ea"
    run_args = calls[-1]
    assert "--conversation" not in run_args
    assert run_args[run_args.index("--add-dir") + 1] == str(tmp_path)
    assert "--dangerously-skip-permissions" in run_args
    assert run_args[run_args.index("--print") + 1] == "go"
    assert kwargs_log[-1]["limit"] == STREAM_READ_LIMIT
    assert kwargs_log[-1]["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs_log[-1]["env"][RUN_ENV] == "run-1"

    deltas = [event for event in events if event.name == "delta"]
    assert [event.message for event in deltas] == [
        "Reading the file",
        " and wrote the result.",
    ]
    command_events = [
        event for event in events if event.kind is AgentEventKind.COMMAND
    ]
    assert [(event.name, event.item_id, event.command) for event in command_events] == [
        ("started", "step-5", "echo shell-ok"),
        ("completed", "step-5", "echo shell-ok"),
        ("completed", "step-6", "echo escaped > ../out.txt"),
    ]
    assert command_events[-1].details["is_error"] is True
    assert command_events[-1].message == "zsh:1: operation not permitted: ../out.txt"
    file_events = [
        event for event in events if event.kind is AgentEventKind.FILE_CHANGE
    ]
    assert [(event.name, event.path) for event in file_events] == [
        ("started", "/workspace/out.txt"),
        ("completed", "/workspace/out.txt"),
    ]
    tool_events = [
        event
        for event in events
        if event.kind is AgentEventKind.TOOL and event.details.get("tool") == "view_file"
    ]
    assert [event.name for event in tool_events] == ["started", "completed"]
    initialized = next(event for event in events if event.name == "initialized")
    assert initialized.details["permission_mode"] == "always-proceed"
    assert initialized.details["cwd"] == "/workspace"


@pytest.mark.asyncio
async def test_existing_session_is_resumed_by_conversation_id(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install(monkeypatch, _StreamProcess(_fixture_lines()), calls=calls)
    adapter = AntigravityStreamJsonAdapter()

    await _run(adapter, _context(tmp_path), [], session_id="conv-9")

    run_args = calls[-1]
    assert run_args[run_args.index("--conversation") + 1] == "conv-9"
    assert "--continue" not in run_args


@pytest.mark.asyncio
async def test_model_wins_over_effort_and_unknown_model_is_dropped(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install(monkeypatch, _StreamProcess(_fixture_lines()), calls=calls)
    adapter = AntigravityStreamJsonAdapter()
    events: list[AgentEvent] = []
    context = _context(
        tmp_path,
        provider_options={"model": "gemini-3.6-flash-low", "effort": "high"},
    )

    await _run(adapter, context, events)

    run_args = calls[-1]
    assert run_args[run_args.index("--model") + 1] == "gemini-3.6-flash-low"
    assert "--effort" not in run_args
    settings = next(event for event in events if event.name == "settings")
    assert settings.details["model"] == "gemini-3.6-flash-low"
    assert settings.details["effort"] == ""
    assert "effort" in settings.details["rejected"]


@pytest.mark.asyncio
async def test_model_missing_from_catalog_is_dropped_and_effort_survives(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install(monkeypatch, _StreamProcess(_fixture_lines()), calls=calls)
    adapter = AntigravityStreamJsonAdapter()
    events: list[AgentEvent] = []
    context = _context(
        tmp_path, provider_options={"model": "made-up-model", "effort": "high"}
    )

    await _run(adapter, context, events)

    run_args = calls[-1]
    assert "--model" not in run_args
    assert run_args[run_args.index("--effort") + 1] == "high"
    settings = next(event for event in events if event.name == "settings")
    assert settings.details["rejected"]["model"] == "not offered by `agy models`"


@pytest.mark.asyncio
async def test_unreadable_model_catalog_skips_validation(monkeypatch, tmp_path) -> None:
    calls: list[tuple[Any, ...]] = []
    _install(
        monkeypatch,
        _StreamProcess(_fixture_lines()),
        models=b"",
        models_returncode=1,
        calls=calls,
    )
    adapter = AntigravityStreamJsonAdapter()

    await _run(
        adapter, _context(tmp_path, provider_options={"model": "unlisted"}), []
    )

    run_args = calls[-1]
    assert run_args[run_args.index("--model") + 1] == "unlisted"


@pytest.mark.asyncio
async def test_probe_subprocesses_use_devnull_stdin(monkeypatch, tmp_path) -> None:
    kwargs_log: list[dict[str, Any]] = []
    calls: list[tuple[Any, ...]] = []
    _install(
        monkeypatch,
        _StreamProcess(_fixture_lines()),
        calls=calls,
        kwargs_log=kwargs_log,
    )
    adapter = AntigravityStreamJsonAdapter()

    await _run(adapter, _context(tmp_path, provider_options={"model": "any"}), [])

    probes = [
        kwargs
        for args, kwargs in zip(calls, kwargs_log)
        if args[-1] in {"--help", "models"}
    ]
    assert {"--help", "models"} <= {args[-1] for args in calls}
    assert all(kwargs["stdin"] is asyncio.subprocess.DEVNULL for kwargs in probes)


@pytest.mark.asyncio
async def test_model_catalog_is_read_once_per_adapter(monkeypatch, tmp_path) -> None:
    calls: list[tuple[Any, ...]] = []

    async def create_process(*args, **kwargs):
        calls.append(args)
        if args[-1] == "--help":
            return _CompletedProcess(stdout=b"", stderr=_HELP_TEXT)
        if args[-1] == "models":
            return _CompletedProcess(stdout=_MODELS_TEXT)
        return _StreamProcess(_fixture_lines())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = AntigravityStreamJsonAdapter()
    context = _context(tmp_path, provider_options={"model": "gemini-3.6-flash-low"})

    await _run(adapter, context, [])
    await _run(adapter, context, [])

    assert [args[-1] for args in calls].count("models") == 1
    assert [args[-1] for args in calls].count("--help") == 1


@pytest.mark.asyncio
async def test_environment_is_isolated_from_write_credentials(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    kwargs_log: list[dict[str, Any]] = []
    calls: list[tuple[Any, ...]] = []
    _install(
        monkeypatch,
        _StreamProcess(_fixture_lines()),
        calls=calls,
        kwargs_log=kwargs_log,
    )
    adapter = AntigravityStreamJsonAdapter()

    await _run(adapter, _context(tmp_path), [])

    env = kwargs_log[-1]["env"]
    assert "GH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env["GH_CONFIG_DIR"] != ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_usage_is_normalized_to_the_shared_keys(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _StreamProcess(_fixture_lines()))
    adapter = AntigravityStreamJsonAdapter()
    events: list[AgentEvent] = []

    result = await _run(adapter, _context(tmp_path), events)

    expected = {
        "input_tokens": 19407,
        "output_tokens": 5,
        "reasoning_output_tokens": 0,
        "cached_input_tokens": 16249,
        "total_tokens": 19412,
    }
    assert result.usage == expected
    turn = next(event for event in events if event.kind is AgentEventKind.TURN)
    assert turn.usage == expected
    usage_event = next(event for event in events if event.kind is AgentEventKind.USAGE)
    assert usage_event.usage == expected
    assert "context_size_tokens" not in result.usage


def test_usage_clamps_and_ignores_unusable_values() -> None:
    assert _usage({"input_tokens": -5, "output_tokens": "x", "thinking_tokens": 7}) == {
        "input_tokens": 0,
        "reasoning_output_tokens": 7,
    }
    assert _usage(None) == {}


@pytest.mark.asyncio
async def test_read_only_turn_records_that_it_is_not_enforced(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install(monkeypatch, _StreamProcess(_fixture_lines()), calls=calls)
    adapter = AntigravityStreamJsonAdapter()
    events: list[AgentEvent] = []

    await _run(adapter, _context(tmp_path, read_only=True), events)

    policy = next(event for event in events if event.name == "policy")
    assert policy.approval == "always-proceed"
    assert policy.details == {"read_only": True, "read_only_enforced": False}
    # `agy` offers no provider-side read-only mode, so the argv is unchanged.
    assert "--dangerously-skip-permissions" in calls[-1]


@pytest.mark.asyncio
async def test_quota_result_is_rate_limited_with_a_retry_hint(
    monkeypatch, tmp_path
) -> None:
    _install(
        monkeypatch,
        _StreamProcess(
            [
                {"event": "init", "conversation_id": "c1", "init": {}},
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "c1",
                        "status": "ERROR",
                        "response": "",
                        "error": (
                            "RESOURCE_EXHAUSTED: Individual quota reached. "
                            "Resets in 1h23m."
                        ),
                    },
                },
            ],
            returncode=1,
        ),
    )
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    error = excinfo.value
    assert error.category is AgentRuntimeErrorCategory.RATE_LIMITED
    assert error.rotate_session is False
    assert error.details["retry_after_text"] == "Resets in 1h23m"
    assert normalize_cli_agent_retry_after(error.details["retry_after_text"]) != ""


def test_result_error_classification() -> None:
    assert _result_error({"status": "SUCCESS"}) is None
    auth = _result_error({"status": "ERROR", "error": "401 Unauthorized"})
    assert auth is not None
    assert auth.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert auth.rotate_session is True
    timed_out = _result_error(
        {"status": "ERROR", "error": "timeout waiting for response"}
    )
    assert timed_out is not None
    assert timed_out.category is AgentRuntimeErrorCategory.PROCESS
    assert timed_out.rotate_session is True
    invalid = _result_error({"status": "ERROR", "error": ""})
    assert invalid is not None
    assert "'ERROR'" in str(invalid)


@pytest.mark.asyncio
async def test_stream_without_a_terminal_result_is_a_protocol_error(
    monkeypatch, tmp_path
) -> None:
    _install(
        monkeypatch,
        _StreamProcess([{"event": "init", "conversation_id": "c1", "init": {}}]),
    )
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_malformed_json_line_is_a_protocol_error(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _StreamProcess([b"{not json"]))
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True


@pytest.mark.asyncio
async def test_empty_response_is_a_protocol_error(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _StreamProcess(
            [
                {"event": "init", "conversation_id": "c1", "init": {}},
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "c1",
                        "status": "SUCCESS",
                        "response": "",
                    },
                },
            ]
        ),
    )
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL


@pytest.mark.asyncio
async def test_terminal_result_survives_a_nonzero_exit(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch, _StreamProcess(_fixture_lines(), returncode=-15, stderr=b"noise")
    )
    adapter = AntigravityStreamJsonAdapter()

    result = await _run(adapter, _context(tmp_path), [])

    assert result.returncode == 0
    assert result.output == "Reading the file and wrote the result."


@pytest.mark.asyncio
async def test_timeout_terminates_the_process_tree(monkeypatch, tmp_path) -> None:
    terminated: list[Any] = []
    _install(monkeypatch, _HangingProcess())
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.antigravity._TIMEOUT_GRACE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.antigravity.terminate_process_tree",
        lambda process, **_: terminated.append(process) or asyncio.sleep(0),
    )
    adapter = AntigravityStreamJsonAdapter(timeout=0.01)

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert terminated


@pytest.mark.asyncio
async def test_oversized_prompt_is_refused_before_launching(
    monkeypatch, tmp_path
) -> None:
    launched: list[tuple[Any, ...]] = []
    _install(monkeypatch, _StreamProcess(_fixture_lines()), calls=launched)
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(
            adapter, _context(tmp_path), [], prompt="x" * (_MAX_PROMPT_BYTES + 1)
        )

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert excinfo.value.details["limit"] == _MAX_PROMPT_BYTES
    # The guard must stay under Linux's per-argv-string cap (MAX_ARG_STRLEN,
    # 128 KiB), or oversized prompts would pass it and die in execve instead.
    assert _MAX_PROMPT_BYTES < 128 * 1024
    assert all(args[-1] == "--help" for args in launched)


@pytest.mark.asyncio
async def test_missing_capabilities_report_unsupported_version(
    monkeypatch, tmp_path
) -> None:
    async def create_process(*args, **kwargs):
        return _CompletedProcess(stderr=b"--print --output-format")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    adapter = AntigravityStreamJsonAdapter()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _run(adapter, _context(tmp_path), [])

    assert excinfo.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert "--conversation" in excinfo.value.details["missing_capabilities"]


def test_decode_events_ignores_steps_with_nothing_to_report() -> None:
    assert (
        _decode_events(
            {"event": "step_update", "step_update": {"step_type": "checkpoint"}}, "c1"
        )
        == []
    )
    assert _decode_events({"event": "unknown"}, "c1") == []
