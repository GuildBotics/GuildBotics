from __future__ import annotations

import asyncio

import pytest

from guildbotics.intelligences.agent_runtime import provider_process
from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.agent_runtime.provider_process import (
    StreamJsonProcess,
    turn_deadline,
)


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
