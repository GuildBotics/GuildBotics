from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from guildbotics.capabilities.task_runs import TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
    _member_cli_command,
    _member_environment,
    _ScopedTokenVerifier,
    _validate_arguments,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
)
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)


def _context(tmp_path: Path, *, read_only: bool = False) -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path / "data" / "workspaces" / "aiko",
        workspace_root=tmp_path / "workspace",
        workspace_data_root=tmp_path / "data",
        conversation_key=ConversationKey("aiko", "grok", "ticket", "issue-1"),
        lease_id="lease-1" if not read_only else "",
        delegation_id="delegation-1" if not read_only else "",
        read_only=read_only,
    )


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.input = b""

    async def communicate(self, value: bytes) -> tuple[bytes, bytes]:
        self.input = value
        self.returncode = 0
        return b'{"ok": true}\n', b""


class _HangingProcess(_Process):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def communicate(self, value: bytes) -> tuple[bytes, bytes]:
        self.input = value
        self.started.set()
        await self.stopped.wait()
        return b"", b"terminated"


@pytest.mark.parametrize(
    ("platform", "executable_name"),
    [("win32", "guildbotics.exe"), ("linux", "guildbotics")],
)
def test_frozen_member_cli_uses_the_platform_executable_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    executable_name: str,
) -> None:
    executable = tmp_path / ".guildbotics" / "bin" / executable_name
    executable.parent.mkdir(parents=True)
    executable.write_text("launcher", encoding="utf-8")
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.sys.frozen",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.sys.platform", platform
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.Path.home",
        lambda: tmp_path,
    )

    assert _member_cli_command() == (str(executable),)


@pytest.mark.asyncio
async def test_execute_uses_fixed_member_entrypoint_and_trusted_environment(
    monkeypatch, tmp_path
) -> None:
    process = _Process()
    launched: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create_agent_subprocess(*argv: str, **kwargs: Any) -> _Process:
        launched.append((argv, kwargs))
        return process

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.create_agent_subprocess",
        create_agent_subprocess,
    )
    context = _context(tmp_path)
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    broker._context = context
    broker._turn_grant = "turn-1"

    result = await broker.execute(
        "turn-1", ["context", "--person", "aiko"], stdin="member input"
    )

    argv, options = launched[0]
    assert argv == (
        "/trusted/guildbotics",
        "member",
        "--workspace",
        str(tmp_path / "workspace"),
        "context",
        "--person",
        "aiko",
    )
    assert options["cwd"] == str(tmp_path / "data" / "workspaces" / "aiko")
    assert options["start_new_session"] is True
    assert options["env"][TASK_RUN_ENV] == "run-1"
    assert options["env"][LEASE_ID_ENV] == "lease-1"
    assert options["env"][DELEGATION_ID_ENV] == "delegation-1"
    assert options["env"][LEASE_PERSON_ENV] == "aiko"
    assert options["env"][LEASE_RUN_ENV] == "run-1"
    assert process.input == b"member input"
    assert result.exit_code == 0
    assert result.stdout == '{"ok": true}\n'


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
    with pytest.raises(ValueError, match=message):
        _validate_arguments(arguments, "aiko")


def test_help_is_the_only_command_that_does_not_require_a_person() -> None:
    _validate_arguments(["help"], "aiko")


def test_read_only_environment_removes_inherited_delegation(
    monkeypatch, tmp_path
) -> None:
    for key in (LEASE_ID_ENV, DELEGATION_ID_ENV, LEASE_PERSON_ENV, LEASE_RUN_ENV):
        monkeypatch.setenv(key, "stale")

    env = _member_environment(_context(tmp_path, read_only=True))

    assert TASK_RUN_ENV in env
    assert all(
        key not in env
        for key in (LEASE_ID_ENV, DELEGATION_ID_ENV, LEASE_PERSON_ENV, LEASE_RUN_ENV)
    )


@pytest.mark.asyncio
async def test_broker_rejects_commands_outside_an_active_turn(tmp_path) -> None:
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))

    with pytest.raises(ValueError, match="No GuildBotics turn"):
        await broker.execute("expired", ["help"])


@pytest.mark.asyncio
async def test_broker_rejects_an_expired_turn_grant(tmp_path) -> None:
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    broker._context = _context(tmp_path)
    broker._turn_grant = "current"

    with pytest.raises(ValueError, match="invalid or expired"):
        await broker.execute("previous", ["help"])


@pytest.mark.asyncio
async def test_activate_normalizes_start_failure(monkeypatch, tmp_path) -> None:
    async def fail_to_start(_broker: MemberCapabilityBroker) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(MemberCapabilityBroker, "_start", fail_to_start)
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))

    with pytest.raises(MemberCapabilityBrokerError, match="could not start") as excinfo:
        await broker.activate(_context(tmp_path))

    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_activate_normalizes_failed_server_task(tmp_path) -> None:
    async def fail() -> None:
        raise OSError("server failed")

    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    broker._serve_task = asyncio.create_task(fail())
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
async def test_http_mcp_requires_bearer_and_dispatches_the_member_tool(
    monkeypatch, tmp_path
) -> None:
    process = _Process()

    async def create_agent_subprocess(*argv: str, **kwargs: Any) -> _Process:
        return process

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.create_agent_subprocess",
        create_agent_subprocess,
    )
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    context = _context(tmp_path)
    await broker.activate(context)
    descriptor = broker.mcp_server

    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer wrong-token"}
        ) as client:
            response = await client.post(
                descriptor["url"],
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

        authorization = descriptor["headers"][0]["value"]
        async with httpx2.AsyncClient(
            headers={"Authorization": authorization}
        ) as client:
            async with streamable_http_client(
                descriptor["url"], http_client=client
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "guildbotics_member",
                        {
                            "turn_grant": broker.turn_grant,
                            "arguments": ["help"],
                        },
                    )
    finally:
        await broker.close()

    assert result.is_error is False
    assert result.structured_content == {
        "exit_code": 0,
        "stdout": '{"ok": true}\n',
        "stderr": "",
    }


@pytest.mark.asyncio
async def test_deactivate_terminates_an_outstanding_member_command(
    monkeypatch, tmp_path
) -> None:
    process = _HangingProcess()

    async def create_agent_subprocess(*argv: str, **kwargs: Any) -> _HangingProcess:
        return process

    async def terminate(candidate: _HangingProcess) -> None:
        assert candidate is process
        candidate.returncode = -15
        candidate.stopped.set()

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.create_agent_subprocess",
        create_agent_subprocess,
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.terminate_process_tree",
        terminate,
    )
    context = _context(tmp_path)
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    broker._context = context
    broker._turn_grant = "turn-1"
    command = asyncio.create_task(broker.execute("turn-1", ["help"]))
    await process.started.wait()

    await broker.deactivate(context)
    result = await command

    assert result.exit_code == -15
    assert broker._context is None
    assert broker._turn_grant == ""


@pytest.mark.asyncio
async def test_cancelled_request_does_not_leave_a_member_process_running(
    monkeypatch, tmp_path
) -> None:
    process = _HangingProcess()
    terminated: list[_HangingProcess] = []

    async def create_agent_subprocess(*argv: str, **kwargs: Any) -> _HangingProcess:
        return process

    async def terminate(candidate: _HangingProcess) -> None:
        terminated.append(candidate)
        candidate.returncode = -15
        candidate.stopped.set()

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.create_agent_subprocess",
        create_agent_subprocess,
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.member_broker.terminate_process_tree",
        terminate,
    )
    broker = MemberCapabilityBroker(command=("/trusted/guildbotics",))
    broker._context = _context(tmp_path)
    broker._turn_grant = "turn-1"
    command = asyncio.create_task(broker.execute("turn-1", ["help"]))
    await process.started.wait()

    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    assert terminated == [process]


@pytest.mark.asyncio
async def test_bearer_token_is_exact_and_scoped() -> None:
    verifier = _ScopedTokenVerifier("expected-token")

    accepted = await verifier.verify_token("expected-token")
    rejected = await verifier.verify_token("wrong-token")

    assert accepted is not None
    assert accepted.scopes == ["member:execute"]
    assert rejected is None


def test_member_environment_preserves_unrelated_host_values(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GUILDBOTICS_TEST_HOST_VALUE", "kept")

    env = _member_environment(_context(tmp_path))

    assert env["GUILDBOTICS_TEST_HOST_VALUE"] == "kept"
    assert env is not os.environ
