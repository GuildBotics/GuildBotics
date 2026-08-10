from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime import environment, windows_job


class _Process:
    def __init__(self, *, pid: int = 42, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            await asyncio.Event().wait()
        return int(self.returncode)

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1

    def terminate(self) -> None:
        pytest.fail("Windows graceful shutdown must not call Process.terminate()")


@pytest.mark.asyncio
async def test_create_agent_subprocess_assigns_before_resume_on_windows(
    monkeypatch,
) -> None:
    events: list[object] = []
    process = _Process()

    class Job:
        @staticmethod
        def create():
            events.append("create-job")
            return Job()

        def assign_and_resume(self, pid: int) -> None:
            events.append(("assign-resume", pid))

        def close(self) -> None:
            events.append("close")

    async def create_process(*program: str, **kwargs: Any):
        events.append(("spawn", program, kwargs))
        return process

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "WindowsJob", Job)
    monkeypatch.setattr(environment.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        environment,
        "register_process_job",
        lambda registered, job: events.append(("register", registered, job)),
    )

    created = await environment.create_agent_subprocess("agent", "run", stdin=-1)

    assert created is process
    assert events[0] == "create-job"
    assert events[1][0] == "spawn"
    assert events[1][2]["creationflags"] == windows_job.creation_flags()
    assert events[2] == ("assign-resume", 42)
    assert events[3][0] == "register"


@pytest.mark.asyncio
async def test_create_agent_subprocess_recovers_suspended_process_on_failure(
    monkeypatch,
) -> None:
    process = _Process()
    events: list[str] = []

    class Job:
        @staticmethod
        def create():
            return Job()

        def assign_and_resume(self, _pid: int) -> None:
            raise OSError("assign failed")

        def terminate(self) -> None:
            events.append("terminate-job")

        def close(self) -> None:
            events.append("close-job")

    async def create_process(*_program: str, **_kwargs: Any):
        return process

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "WindowsJob", Job)
    monkeypatch.setattr(environment.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(OSError, match="assign failed"):
        await environment.create_agent_subprocess("agent")

    assert events == ["terminate-job", "close-job"]
    assert process.killed is True


@pytest.mark.asyncio
async def test_windows_tree_shutdown_waits_then_terminates_job(monkeypatch) -> None:
    process = _Process()
    calls: list[object] = []

    def terminate_job(owned_process) -> bool:
        calls.append(owned_process)
        process.returncode = 1
        return True

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "terminate_process_job", terminate_job)

    await environment.terminate_process_tree(process, grace_seconds=0)

    assert calls == [process]


@pytest.mark.asyncio
async def test_windows_tree_shutdown_terminates_descendants_after_root_exit(
    monkeypatch,
) -> None:
    process = _Process(returncode=0)
    calls: list[object] = []
    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(
        environment,
        "terminate_process_job",
        lambda owned_process: calls.append(owned_process) or True,
    )

    await environment.terminate_process_tree(process)

    assert calls == [process]


@pytest.mark.asyncio
async def test_posix_tree_shutdown_escalates_process_group(monkeypatch) -> None:
    process = _Process()
    calls: list[tuple[int, bool]] = []

    def terminate_group(pid: int, *, force: bool = False) -> None:
        calls.append((pid, force))
        if force:
            process.returncode = 1

    monkeypatch.setattr(environment, "_WINDOWS", False)
    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment, "terminate_posix_process_group", terminate_group)

    await environment.terminate_process_tree(process, grace_seconds=0)

    assert calls == [(42, False), (42, True)]


def test_windows_job_assigns_process_then_resumes_only_thread(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        windows_job,
        "_open_process_for_job",
        lambda pid: events.append(("open", pid)) or 8,
    )
    monkeypatch.setattr(
        windows_job,
        "_assign_process",
        lambda job, process: events.append(("assign", job, process)),
    )
    monkeypatch.setattr(
        windows_job, "_close_handle", lambda handle: events.append(("close", handle))
    )
    monkeypatch.setattr(windows_job, "_thread_ids_for", lambda pid: [pid + 1])
    monkeypatch.setattr(
        windows_job, "_resume_thread", lambda thread: events.append(("resume", thread))
    )

    windows_job.WindowsJob(7).assign_and_resume(42)

    assert events == [
        ("open", 42),
        ("assign", 7, 8),
        ("close", 8),
        ("resume", 43),
    ]


def test_agent_runtime_has_one_subprocess_creation_boundary() -> None:
    runtime_dir = Path(environment.__file__).parent
    direct_calls = {
        path.name: path.read_text(encoding="utf-8").count(
            "asyncio.create_subprocess_exec"
        )
        for path in runtime_dir.glob("*.py")
    }

    assert direct_calls == {
        name: (2 if name == "environment.py" else 0) for name in direct_calls
    }
