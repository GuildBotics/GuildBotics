from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime import provider_process
from guildbotics.intelligences.agent_runtime.host_client import (
    HostCallError,
    HostClient,
    HostTurn,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationKey,
    ConversationRecord,
)
from guildbotics.intelligences.agent_runtime.provider_process import (
    StreamJsonAdapter,
    StreamJsonProcess,
    turn_deadline,
)
from guildbotics.intelligences.agent_runtime.turn import Turn


class _Process:
    """A provider process whose exit the test decides."""

    def __init__(self, *, limit: int = 2**16) -> None:
        self.stdout = asyncio.StreamReader(limit=limit)
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._exited = asyncio.Event()

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def kill(self) -> None:
        if self.returncode is None:
            self.killed = True
            self.exit(-9)


def _output(process: _Process) -> StreamJsonProcess:
    return StreamJsonProcess(process, "Test CLI")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_next_event_reads_objects_and_skips_other_json() -> None:
    process = _Process()
    process.stdout.feed_data(b'[1, 2]\n{"type": "a"}\n"text"\n{"type": "b"}\n')
    process.stdout.feed_eof()
    output = _output(process)

    assert await output.next_event() == {"type": "a"}
    assert await output.next_event() == {"type": "b"}
    assert await output.next_event() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "limit", "message"),
    [
        (b"{not json\n", 2**16, "Malformed Test CLI stream-json event"),
        (b"\xff\xfe\n", 2**16, "Malformed Test CLI stream-json event"),
        (b'{"a": "' + b"x" * 64 + b'"}\n', 16, "could not be read"),
    ],
)
async def test_next_event_reports_unreadable_output_as_a_protocol_error(
    payload: bytes, limit: int, message: str
) -> None:
    process = _Process(limit=limit)
    process.stdout.feed_data(payload)
    process.stdout.feed_eof()

    with pytest.raises(AgentRuntimeError) as excinfo:
        await _output(process).next_event()

    assert excinfo.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert excinfo.value.rotate_session is True
    assert message in str(excinfo.value)


@pytest.mark.asyncio
async def test_finish_observes_the_exit_status_of_a_process_exiting_on_its_own() -> (
    None
):
    process = _Process()
    process.stderr.feed_data(b"  stopped by the provider \n")
    process.stderr.feed_eof()
    output = _output(process)
    # The provider has closed its output but not exited yet.
    asyncio.get_running_loop().call_later(0.05, process.exit, 9)

    await output.finish()

    assert process.killed is False
    assert output.returncode(terminal_seen=False) == 9
    assert output.stderr == "stopped by the provider"


@pytest.mark.asyncio
async def test_finish_ends_a_process_that_outlives_the_grace(monkeypatch) -> None:
    monkeypatch.setattr(provider_process, "_PROCESS_EXIT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(provider_process, "_PIPE_DRAIN_TIMEOUT_SECONDS", 0.01)
    process = _Process()
    output = _output(process)

    await output.finish()

    assert process.killed is True
    assert output.returncode(terminal_seen=False) == -9
    # A terminal result the provider printed outweighs our ending it.
    assert output.returncode(terminal_seen=True) == 0
    # A stderr that never ends is given up on, not waited for.
    assert output.stderr == ""


@pytest.mark.asyncio
async def test_turn_deadline_interrupts_before_reporting_a_timeout() -> None:
    interrupted: list[bool] = []

    async def interrupt() -> None:
        interrupted.append(True)

    with pytest.raises(AgentRuntimeError) as excinfo:
        async with turn_deadline("Test CLI", 0.01, interrupt):
            await asyncio.sleep(1)

    assert interrupted == [True]
    assert excinfo.value.category is AgentRuntimeErrorCategory.PROCESS
    assert excinfo.value.rotate_session is True
    assert str(excinfo.value) == "Test CLI turn timed out."


@pytest.mark.asyncio
async def test_turn_deadline_interrupts_a_cancelled_turn() -> None:
    interrupted: list[bool] = []
    entered = asyncio.Event()

    async def interrupt() -> None:
        interrupted.append(True)

    async def turn() -> None:
        async with turn_deadline("Test CLI", 60, interrupt):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(turn())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert interrupted == [True]


@pytest.mark.asyncio
async def test_turn_deadline_leaves_a_finished_turn_alone() -> None:
    async def interrupt() -> None:
        raise AssertionError("A turn that finished in time is not interrupted.")

    async with turn_deadline("Test CLI", 60, interrupt):
        await asyncio.sleep(0)


class _Unending(HostClient):
    """A window whose host cannot end the turn."""

    def __init__(self) -> None:
        super().__init__("http://window.test/host", "token")

    async def acall(self, name: str, **arguments: Any) -> Any:
        raise HostCallError("unavailable", "The host is not there.")


class _Finishing(StreamJsonAdapter):
    """Finishes its turn in a turn the host cannot end, its provider still
    running until the adapter stops it."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(executable="tool", timeout=60)
        self.tmp_path = tmp_path
        self.stopped = False

    async def _run_active_turn(self, prompt, context, conversation, emit):
        started = HostTurn(
            turn_grant="grant",
            env={},
            cwd=str(self.tmp_path),
            home=str(self.tmp_path),
            mounts={},
            member={"name": "m", "url": "http://broker", "authorization": "a"},
        )
        self._environment = Turn(_Unending(), context, started, {})
        return AgentTerminalResult(
            output="answered", events=(), provider_session_id="s"
        )

    async def _stop_provider(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_a_finished_turn_the_host_cannot_end_keeps_its_result(
    tmp_path, caplog
) -> None:
    """Ending the turn is the host's to finish with the command: the turn
    answered, and its provider is stopped all the same."""
    adapter = _Finishing(tmp_path)
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run",
        cwd=tmp_path,
        conversation_key=ConversationKey("aiko", "tool", "manual", "x"),
    )

    with caplog.at_level(logging.WARNING, logger="guildbotics"):
        result = await adapter.run_turn(
            "hi", context, ConversationRecord(key=context.conversation_key), print
        )

    assert result.output == "answered"
    assert adapter.stopped
    assert "The host is not there." in caplog.text
    assert context.login.refusal() == ""


@pytest.mark.asyncio
async def test_an_interrupt_stops_the_provider_even_if_ending_the_turn_fails() -> None:
    class _Broken:
        async def close(self) -> None:
            raise asyncio.CancelledError

    adapter = _Finishing(Path())
    adapter._environment = _Broken()

    with pytest.raises(asyncio.CancelledError):
        await adapter.interrupt()

    assert adapter.stopped
