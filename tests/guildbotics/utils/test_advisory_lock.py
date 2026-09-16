"""Acquiring an advisory lock must not depend on writing to the lock file."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from guildbotics.utils.advisory_lock import (
    held_lock,
    lock_file_nonblocking,
    open_lock_file,
    read_lock_data,
    unlock_file,
    write_lock_data,
)


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


def test_lock_data_survives_a_lock_file_acquiring_never_wrote(tmp_path: Path) -> None:
    """The byte reserved for locking is written by the data writer, not by acquiring."""
    path = tmp_path / "advisory.lock"
    with held_lock(path) as handle:
        assert read_lock_data(handle) == ""
        write_lock_data(handle, "payload\n")
        assert read_lock_data(handle) == "payload\n"

    with held_lock(path) as handle:
        assert read_lock_data(handle) == "payload\n"
