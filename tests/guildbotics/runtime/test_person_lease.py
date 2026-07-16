from __future__ import annotations

import os

import pytest

from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
    PersonExecutionLease,
    PersonLeaseUnavailableError,
    current_person_lease,
    delegation_environment,
    validate_delegation,
)


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


def test_nested_delegation_requires_exact_locked_metadata(tmp_path) -> None:
    lease = PersonExecutionLease("aiko", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    env = delegation_environment("run-1")

    assert (
        validate_delegation("aiko", data_root=tmp_path, environ=env) == lease.metadata
    )

    forged = dict(env)
    forged[DELEGATION_ID_ENV] = "forged"
    assert validate_delegation("aiko", data_root=tmp_path, environ=forged) is None
    assert validate_delegation("yuki", data_root=tmp_path, environ=env) is None
    assert env == {
        LEASE_ID_ENV: lease.metadata.lease_id,
        DELEGATION_ID_ENV: lease.metadata.delegation_id,
        LEASE_PERSON_ENV: "aiko",
        LEASE_RUN_ENV: "run-1",
    }

    lease.release()
    assert validate_delegation("aiko", data_root=tmp_path, environ=env) is None


def test_completed_delegation_can_bind_a_later_native_run(tmp_path) -> None:
    lease = PersonExecutionLease("aiko", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")

    first = delegation_environment("run-1")
    lease.unbind_run_id("run-1")
    second = delegation_environment("run-2")

    assert first[LEASE_RUN_ENV] == "run-1"
    assert second[LEASE_RUN_ENV] == "run-2"
    assert first[LEASE_ID_ENV] == second[LEASE_ID_ENV]
    assert validate_delegation("aiko", data_root=tmp_path, environ=first) is None
    assert validate_delegation("aiko", data_root=tmp_path, environ=second) is not None
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
