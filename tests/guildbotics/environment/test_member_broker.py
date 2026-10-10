from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.requests import Request

from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.environment import member_broker
from guildbotics.environment.loopback_server import LoopbackServer
from guildbotics.environment.member_broker import (
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
    _rejection_reason,
    _ScopedTokenVerifier,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
)
from guildbotics.runtime.member_invocation import ChatSubject, MemberInvocation, Work
from guildbotics.runtime.person_lease import PersonExecutionLease

#: The module, not the `member` group `guildbotics.cli` re-exports by that name.
_MEMBER_CLI = importlib.import_module("guildbotics.cli.member")


def _context(
    tmp_path: Path, *, work: Work = Work.of_ticket("https://example.test/1")
) -> AgentExecutionContext:
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path / "data" / "workspaces" / "aiko",
        conversation_key=ConversationKey("aiko", "grok", work.kind, work.identity),
        lease=PersonExecutionLease("aiko", tmp_path),
        participant_labels='{"U1":"aiko"}',
        trace_id="trace-parent",
        work=work,
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
    ("work", "run_id", "task_run_id"),
    [
        (Work.of_ticket("https://example.test/1"), "", "run-1"),
        (Work.of_chat(ChatSubject("slack", "C1", "1.0", "C1:1.1", "U1")), "run-1", ""),
    ],
)
async def test_execute_runs_the_member_command_in_this_process_for_the_turn(
    monkeypatch, tmp_path, work: Work, run_id: str, task_run_id: str
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
    context = _context(tmp_path, work=work)
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
                work=work,
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


@pytest.mark.asyncio
async def test_each_member_command_is_handed_the_commands_environment(
    monkeypatch, tmp_path
) -> None:
    """For as long as the broker waits for the command, and no longer: git
    the command runs in the microVM ends when the broker stops waiting."""
    handed: list[Any] = []

    class Guest:
        def until(self, deadline: float) -> tuple[str, float]:
            return ("guest", deadline)

    def run_in_process(arguments, invocation, *, cwd, stdin):
        handed.append(invocation.guest)
        return 0, "", ""

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    broker = _active_broker(_context(tmp_path))
    broker._guest = cast(Any, Guest())
    before = time.monotonic()

    await broker.execute("turn-1", ["context", "--person", "aiko"])

    [(guest, deadline)] = handed
    assert guest == "guest"
    timeout = member_broker._COMMAND_TIMEOUT_SECONDS
    assert before + timeout <= deadline <= time.monotonic() + timeout


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
@pytest.mark.parametrize("transport", ["mcp", "host"])
@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("abandon", [False, True])
async def test_abandon_waits_for_member_writes_even_after_timeout(
    monkeypatch, tmp_path, transport: str, timed_out: bool, fails: bool, abandon: bool
) -> None:
    """Both member entry points retain writes until command cancellation ends."""
    started = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    writes: list[str] = []
    loop = asyncio.get_running_loop()

    def run_in_process(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        writes.append("member write")
        finished.set()
        if fails:
            raise ValueError("worker failed")
        return 0, "done", ""

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    monkeypatch.setattr(
        member_broker, "_COMMAND_TIMEOUT_SECONDS", 0.05 if timed_out else 30
    )
    broker = _active_broker(_context(tmp_path))

    async def host(_call, _arguments):
        return await broker.run(
            "aiko", ["help"], MemberInvocation(), cwd=tmp_path, stdin=""
        )

    broker.serve(host, contextvars.Context())
    if transport == "mcp":
        call = asyncio.create_task(broker.execute("turn-1", ["help"]))
    else:

        async def receive():
            return {"type": "http.request", "body": b"{}"}

        request = Request({"type": "http", "path_params": {"call": "member"}}, receive)
        call = asyncio.create_task(broker._answer(request))

    async def never():
        await asyncio.Event().wait()

    waiting = asyncio.create_task(never())
    broker._calls.add(waiting)
    waiting.add_done_callback(broker._calls.discard)
    closing = following = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        if transport == "mcp" and not timed_out:
            following = asyncio.create_task(broker.execute("turn-1", ["help"]))
            await asyncio.sleep(0)
        if timed_out:
            if transport == "mcp":
                assert (await asyncio.wait_for(call, 5)).exit_code == 124
            else:
                # The host call and member command share a deadline: either
                # the outer host timeout or the inner member timeout can win.
                try:
                    result = await asyncio.wait_for(call, 5)
                except member_broker.HostCallError as exc:
                    assert "timed out" in str(exc)
                else:
                    assert result.exit_code == 124
        closing = asyncio.create_task(broker.settle(abandon=abandon))
        await asyncio.sleep(0.05)
        assert not closing.done()
        assert writes == []
        # A second stop request must not cancel the worker being drained.
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        assert waiting.cancelled()
    finally:
        release.set()
        waiting.cancel()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await asyncio.gather(call, return_exceptions=True)
        await asyncio.gather(waiting, return_exceptions=True)
        if following is not None:
            result = await asyncio.wait_for(following, 5)
        # Also clean up a baseline implementation that ends settlement early.
        assert await asyncio.to_thread(finished.wait, 5)

    assert writes == ["member write"]
    if following is not None:
        assert result.exit_code == 2
    assert broker._calls == set()
    assert (await broker.execute("turn-1", ["help"])).exit_code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "host"])
@pytest.mark.parametrize(
    ("stop", "timed_out"),
    [
        ("abandon", False),
        ("cancel_settle", False),
        ("abandon", True),
        ("cancel_settle", True),
        ("timeout", True),
        ("cancel_request", False),
        ("finish", False),
    ],
)
async def test_queued_member_writes_follow_request_lifetime(
    monkeypatch, tmp_path, transport: str, stop: str, timed_out: bool
) -> None:
    """Only live requests start queued commands; normal settlement waits them."""
    release = threading.Event()
    submitted = asyncio.Event()
    writes: list[str] = []
    loop = asyncio.get_running_loop()
    submit = loop.run_in_executor

    def run_in_process(*_args, **_kwargs):
        writes.append("member write")
        return 0, "", ""

    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    monkeypatch.setattr(
        member_broker, "_COMMAND_TIMEOUT_SECONDS", 0.05 if timed_out else 30
    )
    broker = _active_broker(_context(tmp_path))

    async def host(_call, _arguments):
        return await broker.run(
            "aiko", ["help"], MemberInvocation(), cwd=tmp_path, stdin=""
        )

    broker.serve(host, contextvars.Context())

    async def never():
        await asyncio.Event().wait()

    waiting = None
    if stop == "cancel_settle":
        waiting = asyncio.create_task(never())
        broker._calls.add(waiting)
        waiting.add_done_callback(broker._calls.discard)
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(release.wait, 10)

        def enqueue(_executor, function, *args):
            work = submit(executor, function, *args)
            submitted.set()
            return work

        monkeypatch.setattr(loop, "run_in_executor", enqueue)
        if transport == "mcp":
            call = asyncio.create_task(broker.execute("turn-1", ["help"]))
        else:

            async def receive():
                return {"type": "http.request", "body": b"{}"}

            request = Request(
                {"type": "http", "path_params": {"call": "member"}}, receive
            )
            call = asyncio.create_task(broker._answer(request))
        closing = None
        try:
            await asyncio.wait_for(submitted.wait(), 5)
            workers = tuple(broker._workers)
            if timed_out:
                try:
                    result = await asyncio.wait_for(call, 5)
                except member_broker.HostCallError as exc:
                    assert transport == "host" and "timed out" in str(exc)
                else:
                    assert result.exit_code == 124
            if stop == "cancel_request":
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
            closing = asyncio.create_task(broker.settle(abandon=stop == "abandon"))
            await asyncio.sleep(0.05)
            assert closing.done() == (stop in {"timeout", "cancel_request"})
            assert writes == []
            if stop in {"abandon", "cancel_settle"}:
                for _ in range(3):
                    closing.cancel()
                    await asyncio.sleep(0)
                    assert not closing.done()
                if waiting is not None:
                    assert waiting.cancelled()
        finally:
            release.set()
            if waiting is not None:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            if closing is not None:
                await asyncio.wait_for(
                    asyncio.gather(closing, return_exceptions=True), 5
                )
            await asyncio.gather(call, return_exceptions=True)
            assert blocker.result(timeout=5)
    await asyncio.gather(*workers, return_exceptions=True)
    assert writes == (["member write"] if stop == "finish" else [])
    assert broker._workers == {}
    assert broker._calls == set()


@pytest.mark.asyncio
async def test_cancel_calls_stops_all_queued_workers_before_yielding(
    monkeypatch, tmp_path
) -> None:
    """Executor workers can start before cancelled requests resume on the loop."""
    loop = asyncio.get_running_loop()
    queued = []
    writes: list[str] = []

    def enqueue(_executor, function, *args):
        work = loop.create_future()
        queued.append((work, partial(function, *args)))
        return work

    def run_in_process(*_args, **_kwargs):
        writes.append("member write")
        return 0, "", ""

    monkeypatch.setattr(loop, "run_in_executor", enqueue)
    monkeypatch.setattr(_MEMBER_CLI, "run_in_process", run_in_process)
    broker = _active_broker(_context(tmp_path))
    calls = [
        asyncio.create_task(
            broker.run("aiko", ["help"], MemberInvocation(), cwd=tmp_path, stdin="")
        )
        for _ in range(2)
    ]
    try:
        await asyncio.sleep(0)
        assert len(queued) == len(broker._workers) == len(broker._calls) == 2
        events = tuple(broker._workers.values())
        assert len({id(event) for event in events}) == 2
        assert not any(event.is_set() for event in events)

        broker._cancel_calls()
        # Do not yield: request cancellation handlers must not notify for us.
        results = [command() for _work, command in queued]
        assert all(event.is_set() for event in events)
        assert [result[0] for result in results] == [125, 125]
        assert writes == []
        assert all(not work.cancelled() for work, _command in queued)
    finally:
        for work, _command in queued:
            work.set_result((125, "", ""))
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await broker.settle(abandon=True)

    assert broker._workers == {}
    assert broker._calls == set()


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
    endpoint = broker.endpoint
    turn_grant = broker.turn_grant

    try:
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
        authorization = endpoint.authorization
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

        authorization = endpoint.authorization
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
