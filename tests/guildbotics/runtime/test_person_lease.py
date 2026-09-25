from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from guildbotics.runtime.person_lease import (
    PersonExecutionLease,
    PersonLeaseUnavailableError,
    current_person_lease,
)


def _attempt_person_lease(data_root: str, connection) -> None:
    lease = PersonExecutionLease("aiko", Path(data_root))
    try:
        lease.acquire(source="manual", command="child", work_id="child-work")
    except PersonLeaseUnavailableError:
        connection.send("blocked")
    else:
        connection.send("acquired")
        lease.release()
    finally:
        connection.close()


def _child_lease_result(data_root: Path) -> str:
    process_context = multiprocessing.get_context("spawn")
    receiver, sender = process_context.Pipe(duplex=False)
    process = process_context.Process(
        target=_attempt_person_lease,
        args=(str(data_root), sender),
    )
    process.start()
    sender.close()
    try:
        assert receiver.poll(5.0), "child process did not report its lease result"
        result = receiver.recv()
    finally:
        receiver.close()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0
    return result


def test_person_lease_serializes_same_person_and_allows_other_person(tmp_path) -> None:
    first = PersonExecutionLease("aiko", tmp_path)
    first.acquire(source="routine", command="ticket", work_id="work-1")
    second = PersonExecutionLease("aiko", tmp_path)

    with pytest.raises(PersonLeaseUnavailableError) as excinfo:
        second.acquire(source="manual", command="chat", work_id="work-2")

    assert excinfo.value.metadata is not None
    assert excinfo.value.metadata.work_id == "work-1"
    other = PersonExecutionLease("yuki", tmp_path)
    other.acquire(source="manual", command="chat", work_id="work-3")
    other.release()
    first.release()
    assert current_person_lease() is None


def test_person_lease_serializes_across_processes(tmp_path: Path) -> None:
    parent = PersonExecutionLease("aiko", tmp_path)
    parent.acquire(source="routine", command="ticket", work_id="parent-work")
    try:
        assert _child_lease_result(tmp_path) == "blocked"
    finally:
        parent.release()

    assert _child_lease_result(tmp_path) == "acquired"


def test_a_lease_binds_one_run_at_a_time(tmp_path) -> None:
    lease = PersonExecutionLease("aiko", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    lease_id = lease.bind_run_id("run-1").lease_id

    with pytest.raises(RuntimeError, match="another run id"):
        lease.bind_run_id("run-2")
    lease.unbind_run_id("run-1")

    assert lease.bind_run_id("run-2").run_id == "run-2"
    assert lease.metadata.lease_id == lease_id
    lease.release()


def test_stale_lock_file_is_reclaimed(tmp_path) -> None:
    lease = PersonExecutionLease("aiko", tmp_path)
    lease.path.parent.mkdir(parents=True)
    lease.path.write_text(
        '{"pid":999999,"person_id":"aiko","lease_id":"old"}\n',
        encoding="utf-8",
    )

    metadata = lease.acquire(source="manual", command="new", work_id="work-1")

    assert metadata.pid == os.getpid()
    lease.release()
