"""The processes a command and its turns start, as they wait for and stop them."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from guildbotics.utils import child_process
from guildbotics.utils.child_process import ChildProcess

#: A process that starts one of its own, holding its pipes, says that process'
#: id and something on its standard error, and then runs on (``killed``) or
#: ends by itself -- after saying what it read, when it reads (``reads``).
_LEAVES_ONE_RUNNING = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
# The parent stops this process as soon as it has read the pid. Flush first,
# or that stop lands before the write and the reader sees an empty stream.
print("said before it ended", file=sys.stderr, flush=True)
print(child.pid, flush=True)
if sys.argv[1] == "killed":
    time.sleep(60)
if sys.argv[1] == "reads":
    print(sys.stdin.read(), flush=True)
"""


async def _start(how: str) -> ChildProcess:
    return await ChildProcess.start(
        sys.executable,
        "-c",
        _LEAVES_ONE_RUNNING,
        how,
        cwd=os.getcwd(),
        env=os.environ,
    )


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch) -> None:
    monkeypatch.setattr(child_process, "_PIPES_OUTLIVE_SECONDS", 0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["killed", "ends"])
async def test_a_process_ends_with_itself_not_what_it_left_holding_its_pipes(how):
    """Stopping or waiting for a process ends with the process, and its output
    ends shortly after: a process it left running, which holds its pipes open,
    holds neither whoever waits for it nor whoever reads what it said."""
    process = await _start(how)
    # The pipes close shortly after the process ends. A read started only then
    # loses bytes still in the kernel buffer when that delay is shorter than
    # the time a loaded runner takes to reach the read.
    stderr = asyncio.create_task(process.stderr.read())
    rest = None
    left = None
    try:
        left = int(await process.stdout.readline())
        rest = asyncio.create_task(process.stdout.read())
        if how == "killed":
            await asyncio.wait_for(process.kill(), 10)
        else:
            await asyncio.wait_for(process.wait(), 10)

        assert process.returncode is not None
        assert await asyncio.wait_for(rest, 10) == b""
        said = await asyncio.wait_for(stderr, 10)
        assert said.decode().strip() == "said before it ended"
    finally:
        if rest is not None:
            rest.cancel()
        stderr.cancel()
        if left is not None:
            os.kill(left, signal.SIGTERM)


@pytest.mark.asyncio
async def test_communicate_gives_what_it_said_when_it_ends():
    """What a process is given reaches it, and what it said comes back when it
    ends, not when what it left running does."""
    process = await _start("reads")

    stdout, stderr = await asyncio.wait_for(process.communicate(b"given"), 10)

    left, said = stdout.decode().split()
    os.kill(int(left), signal.SIGTERM)
    assert said == "given"
    assert stderr.decode().strip() == "said before it ended"
    assert process.returncode == 0
