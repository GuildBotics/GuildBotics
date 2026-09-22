"""Acquiring an advisory lock must not depend on writing to the lock file."""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest

import guildbotics.utils.advisory_lock as advisory_lock_module
from guildbotics.utils.advisory_lock import (
    LockTimeoutError,
    held_lock,
    lock_file_nonblocking,
    open_lock_file,
    read_lock_data,
    unlock_file,
    write_lock_data,
)


def _attempt_lock(path: str, connection) -> None:
    try:
        with held_lock(Path(path), timeout=0.2):
            connection.send("acquired")
    except LockTimeoutError:
        connection.send("blocked")
    finally:
        connection.close()


def _child_lock_result(path: Path) -> str:
    process_context = multiprocessing.get_context("spawn")
    receiver, sender = process_context.Pipe(duplex=False)
    process = process_context.Process(target=_attempt_lock, args=(str(path), sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(5.0), "child process did not report its lock result"
        result = receiver.recv()
    finally:
        receiver.close()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0
    return result


def test_a_lock_file_nothing_has_written_to_is_still_exclusive(tmp_path: Path) -> None:
    """The first holder locks an empty file, and the second is told to wait.

    Windows locks a byte range rather than the file, so a lock file that has
    never been written to is the ordinary case rather than a special one. A
    holder that created the byte instead would leave the loser of that race
    refused with an error meaning failure rather than contention.
    """
    path = tmp_path / "advisory.lock"
    first = open_lock_file(path)
    second = open_lock_file(path)
    try:
        lock_file_nonblocking(first)

        with pytest.raises(BlockingIOError):
            lock_file_nonblocking(second)

        unlock_file(first)
        lock_file_nonblocking(second)
        unlock_file(second)
    finally:
        second.close()
        first.close()


def test_a_refused_holder_closes_without_raising(tmp_path: Path) -> None:
    """Nothing stays buffered on the handle that lost, so closing it is quiet."""
    path = tmp_path / "advisory.lock"
    first = open_lock_file(path)
    second = open_lock_file(path)
    try:
        lock_file_nonblocking(first)
        with pytest.raises(BlockingIOError):
            lock_file_nonblocking(second)
    finally:
        second.close()
        unlock_file(first)
        first.close()


def test_concurrent_first_acquisitions_all_succeed(tmp_path: Path) -> None:
    """Threads reaching a lock file that does not exist yet take turns."""
    path = tmp_path / "advisory.lock"
    start = threading.Barrier(4)
    failures: list[BaseException] = []
    entered: list[int] = []

    def acquire(index: int) -> None:
        start.wait()
        try:
            with held_lock(path, timeout=10.0):
                entered.append(index)
        except BaseException as exc:  # pragma: no cover - failure detail
            failures.append(exc)

    threads = [threading.Thread(target=acquire, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(entered) == [0, 1, 2, 3]


def test_threads_are_serialized_without_os_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process mutex covers systems where file locks do not exclude threads."""
    monkeypatch.setattr(advisory_lock_module, "lock_file_nonblocking", lambda _: None)
    monkeypatch.setattr(advisory_lock_module, "unlock_file", lambda _: None)
    path = tmp_path / "advisory.lock"
    start = threading.Barrier(2)
    overlap = threading.Barrier(2)
    overlapped: list[bool] = []

    def acquire() -> None:
        start.wait()
        with held_lock(path):
            try:
                overlap.wait(0.2)
            except threading.BrokenBarrierError:
                return
            overlapped.append(True)

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not overlapped


def test_a_lock_excludes_another_process(tmp_path: Path) -> None:
    path = tmp_path / "advisory.lock"

    with held_lock(path):
        assert _child_lock_result(path) == "blocked"

    assert _child_lock_result(path) == "acquired"


def test_lock_data_survives_a_lock_file_acquiring_never_wrote(tmp_path: Path) -> None:
    """The byte reserved for locking is written by the data writer, not by acquiring."""
    path = tmp_path / "advisory.lock"
    with held_lock(path) as handle:
        assert read_lock_data(handle) == ""
        write_lock_data(handle, "payload\n")
        assert read_lock_data(handle) == "payload\n"

    with held_lock(path) as handle:
        assert read_lock_data(handle) == "payload\n"
