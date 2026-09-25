from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_runtime import member_broker
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
    _rejection_reason,
    _ScopedTokenVerifier,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
)
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.runtime.member_invocation import MemberInvocation
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils.loopback_server import LoopbackServer

#: The module, not the `member` group `guildbotics.cli` re-exports by that name.
_MEMBER_CLI = importlib.import_module("guildbotics.cli.member")


def _context(tmp_path: Path, *, work_kind: str = "ticket") -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path / "data" / "workspaces" / "aiko",
        workspace_root=tmp_path / "workspace",
        workspace_data_root=tmp_path / "data",
        conversation_key=ConversationKey("aiko", "grok", work_kind, "work-1"),
        lease=PersonExecutionLease("aiko", tmp_path),
        participant_labels='{"U1":"aiko"}',
        trace_id="trace-parent",
        contract=AccessContract(),
    )


def _active_broker(context: AgentExecutionContext) -> MemberCapabilityBroker:
    broker = MemberCapabilityBroker()
    broker._context = context
    broker._turn_grant = "turn-1"
    return broker


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_kind", "run_id", "task_run_id"),
    [("ticket", "", "run-1"), ("chat", "run-1", "")],
)
async def test_execute_runs_the_member_command_in_this_process_for_the_turn(
    monkeypatch, tmp_path, work_kind: str, run_id: str, task_run_id: str
) -> None:
    calls: list[dict[str, Any]] = []

    def run_in_process(arguments, invocation, *, cwd, stdin):
        calls.append(
            {
                "arguments": arguments,
                "invocation": invocation,
                "cwd": cwd,
                "stdin": stdin,
                "thread": threading.get_ident(),
            }
        )
        return 3, "out", "err"

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    context = _context(tmp_path, work_kind=work_kind)
    broker = _active_broker(context)

    result = await broker.execute(
        "turn-1", ["context", "--person", "aiko"], stdin="member input"
    )

    assert calls == [
        {
            "arguments": ["context", "--person", "aiko"],
            "invocation": MemberInvocation(
                run_id=run_id,
                task_run_id=task_run_id,
                participant_labels='{"U1":"aiko"}',
                trace_id="trace-parent",
                lease=context.lease,
            ),
            "cwd": context.cwd,
            "stdin": "member input",
            "thread": calls[0]["thread"],
        }
    ]
    # A worker thread of its own: Click and asyncio.run need one per command.
    assert calls[0]["thread"] != threading.get_ident()
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out", "err")


_INHERITED: contextvars.ContextVar[str] = contextvars.ContextVar(
    "inherited", default=""
)


@pytest.mark.asyncio
async def test_member_command_starts_from_an_empty_context(
    monkeypatch, tmp_path
) -> None:
    """The broker's server task holds what the turn that started it had bound;
    a command must see only the invocation it is handed."""
    seen: list[str] = []

    def run_in_process(arguments, invocation, *, cwd, stdin):
        seen.append(_INHERITED.get())
        return 0, "", ""

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    broker = _active_broker(_context(tmp_path))
    token = _INHERITED.set("an earlier turn")
    try:
        await broker.execute("turn-1", ["help"])
    finally:
        _INHERITED.reset(token)

    assert seen == [""]


@pytest.mark.asyncio
async def test_a_command_that_times_out_is_reported_and_frees_the_turn(
    monkeypatch, tmp_path
) -> None:
    release = threading.Event()
    started: list[list[str]] = []

    def run_in_process(arguments, invocation, *, cwd, stdin):
        started.append(arguments)
        if arguments == ["hang"]:
            release.wait(5)
        return 0, "done", ""

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    monkeypatch.setattr(member_broker, "_COMMAND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(member_broker, "_rejection_reason", lambda *_: None)
    broker = _active_broker(_context(tmp_path))
    try:
        timed_out = await broker.execute("turn-1", ["hang"])
        following = await broker.execute("turn-1", ["help"])
    finally:
        release.set()

    assert timed_out.exit_code == 124
    assert "may still complete" in timed_out.stderr
    assert (following.exit_code, following.stdout) == (0, "done")
    assert started == [["hang"], ["help"]]


@pytest.mark.asyncio
async def test_output_beyond_the_limit_is_truncated(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(member_broker, "_MAX_OUTPUT_BYTES", 4)
    monkeypatch.setattr(
        _MEMBER_CLI,
        "run_in_process",
        lambda *_args, **_kwargs: (0, "abcdef", "ab"),
    )
    broker = _active_broker(_context(tmp_path))

    result = await broker.execute("turn-1", ["help"])

    assert result.stdout == "abcd\n[output truncated]"
    assert result.stderr == "ab"


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["context", "--person", "yuki"], "another person"),
        (["context", "--person", "aiko", "--workspace", "/tmp"], "overridden"),
        (["context"], "--person"),
        (["context", "--person", "aiko\0other"], "NUL"),
    ],
)
def test_arguments_are_scoped_to_active_person_and_workspace(
    arguments: list[str], message: str
) -> None:
    reason = _rejection_reason(arguments, "aiko")

    assert reason is not None
    assert message in reason


def test_help_is_the_only_command_that_does_not_require_a_person() -> None:
    assert _rejection_reason(["help"], "aiko") is None


@pytest.mark.asyncio
async def test_broker_rejects_commands_outside_an_active_turn(tmp_path) -> None:
    """Rejections stay in-band; a raised MCP tool error would fail the turn."""
    broker = MemberCapabilityBroker()

    result = await broker.execute("expired", ["help"])

    assert result.exit_code == 2
    assert "No GuildBotics turn" in result.stderr
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_broker_rejects_an_expired_turn_grant(tmp_path) -> None:
    broker = MemberCapabilityBroker()
    broker._context = _context(tmp_path)
    broker._turn_grant = "current"

    result = await broker.execute("previous", ["help"])

    assert result.exit_code == 2
    assert "invalid or expired" in result.stderr


@pytest.mark.asyncio
async def test_activate_normalizes_start_failure(monkeypatch, tmp_path) -> None:
    async def fail_to_start(_broker: MemberCapabilityBroker) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(MemberCapabilityBroker, "_start", fail_to_start)
    broker = MemberCapabilityBroker()

    with pytest.raises(MemberCapabilityBrokerError, match="could not start") as excinfo:
        await broker.activate(_context(tmp_path))

    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_activate_normalizes_failed_server_task(tmp_path) -> None:
    async def fail() -> None:
        raise OSError("server failed")

    broker = MemberCapabilityBroker()
    broker._server = LoopbackServer(
        cast(Any, None), asyncio.create_task(fail()), port=0
    )
    await asyncio.sleep(0)

    with pytest.raises(
        MemberCapabilityBrokerError, match="stopped unexpectedly"
    ) as excinfo:
        await broker.activate(_context(tmp_path))

    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _can_bind_localhost(), reason="Environment cannot bind a local TCP socket."
)
async def test_the_broker_leaves_the_process_logging_as_it_was(
    monkeypatch, tmp_path
) -> None:
    """The MCP server it runs would set the root logger up as it is made --
    a level and a handler of its own, which every library's records then
    reach. The process's logging is not the broker's to set."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    broker = MemberCapabilityBroker()
    context = _context(tmp_path)
    await broker.activate(context)
    await broker.deactivate(context)

    assert root.handlers == []
    assert root.level == logging.WARNING


def test_brokers_starting_at_once_leave_the_process_logging_as_it_was(
    monkeypatch,
) -> None:
    """Members' brokers start on threads of their own: one must not take the
    root logger another's MCPServer set for the one it found."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    set_up = threading.Event()
    found: list[list[logging.Handler]] = []

    def first() -> None:
        with member_broker._root_logging_kept():
            logging.basicConfig(level=logging.INFO)  # As MCPServer does.
            set_up.set()
            time.sleep(0.2)

    def second() -> None:
        set_up.wait(5)
        with member_broker._root_logging_kept():
            found.append(root.handlers[:])

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert found == [[]]
    assert root.handlers == [] and root.level == logging.WARNING


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _can_bind_localhost(), reason="Environment cannot bind a local TCP socket."
)
async def test_http_mcp_requires_bearer_and_dispatches_the_member_tool(
    tmp_path,
) -> None:
    broker = MemberCapabilityBroker()
    context = _context(tmp_path)
    await broker.activate(context)
    descriptor = broker.mcp_server
    endpoint = broker.endpoint
    turn_grant = broker.turn_grant

    try:
        # The descriptor a provider reads inside the environment names the
        # broker by the gateway's alias; this host-side test reaches it by
        # the loopback address the same port answers on.
        assert descriptor["url"] == endpoint.guest_url
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer wrong-token"}
        ) as client:
            response = await client.post(
                endpoint.url,
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 401

        # A turn inside the agent environment names this host by the
        # gateway's alias; the Host check, which runs behind the bearer
        # check, lets it through and still refuses any other name.
        authorization = descriptor["headers"][0]["value"]
        assert endpoint.port == int(endpoint.url.rsplit(":", 1)[1].split("/")[0])
        assert (
            endpoint.guest_url
            == f"http://host.microsandbox.internal:{endpoint.port}/mcp"
        )
        async with httpx2.AsyncClient(
            headers={"Authorization": authorization}
        ) as client:
            for host, expected in (
                (f"host.microsandbox.internal:{endpoint.port}", 200),
                (f"evil.example:{endpoint.port}", 421),
            ):
                response = await client.post(
                    endpoint.url,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Host": host,
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                assert response.status_code == expected, host

        authorization = descriptor["headers"][0]["value"]
        async with (
            httpx2.AsyncClient(headers={"Authorization": authorization}) as client,
            streamable_http_client(endpoint.url, http_client=client) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "guildbotics_member",
                {
                    "turn_grant": broker.turn_grant,
                    "arguments": ["help"],
                },
            )
            # A refused request must stay a structured result: an MCP
            # tool error here fails the entire Antigravity run status.
            rejected = await session.call_tool(
                "guildbotics_member",
                {
                    "turn_grant": "stale-grant",
                    "arguments": ["help"],
                },
            )
    finally:
        await broker.close()

    assert result.is_error is False
    assert result.structured_content == {
        "exit_code": 0,
        "stdout": capability_reference_text() + "\n",
        "stderr": "",
    }
    returned = json.dumps(result.structured_content)
    assert authorization not in returned
    assert turn_grant not in returned
    assert rejected.is_error is False
    assert rejected.structured_content is not None
    assert rejected.structured_content["exit_code"] == 2
    assert "invalid or expired" in rejected.structured_content["stderr"]


@pytest.mark.asyncio
async def test_bearer_token_is_exact_and_scoped() -> None:
    verifier = _ScopedTokenVerifier("expected-token")

    accepted = await verifier.verify_token("expected-token")
    rejected = await verifier.verify_token("wrong-token")

    assert accepted is not None
    assert accepted.scopes == ["member:execute"]
    assert rejected is None
