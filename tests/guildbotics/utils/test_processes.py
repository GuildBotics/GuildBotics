from __future__ import annotations

import errno

import pytest

from guildbotics.utils import processes


def test_pid_exists_windows_uses_open_process_without_os_kill(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    closed: list[int] = []

    monkeypatch.setattr(processes, "_WINDOWS", True)
    monkeypatch.setattr(
        processes,
        "_open_windows_process",
        lambda access, pid: calls.append((access, pid)) or 42,
    )
    monkeypatch.setattr(
        processes, "_close_windows_handle", lambda handle: closed.append(handle)
    )
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda *_args: pytest.fail("Windows existence checks must not call os.kill"),
    )

    assert processes.pid_exists(1234) is True
    assert calls == [(processes._PROCESS_QUERY_LIMITED_INFORMATION, 1234)]
    assert closed == [42]


def test_pid_exists_windows_treats_access_denied_as_existing(monkeypatch) -> None:
    monkeypatch.setattr(processes, "_WINDOWS", True)
    monkeypatch.setattr(processes, "_open_windows_process", lambda _access, _pid: 0)
    monkeypatch.setattr(
        processes, "_windows_last_error", lambda: processes._ERROR_ACCESS_DENIED
    )

    assert processes.pid_exists(1234) is True


def test_pid_exists_posix_handles_missing_and_permission_denied(monkeypatch) -> None:
    monkeypatch.setattr(processes, "_WINDOWS", False)

    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert processes.pid_exists(1234) is False

    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(
            PermissionError(errno.EPERM, "denied")
        ),
    )
    assert processes.pid_exists(1234) is True


def test_force_terminate_pid_windows_uses_terminate_process(monkeypatch) -> None:
    terminated: list[tuple[int, int]] = []
    closed: list[int] = []

    class Kernel32:
        @staticmethod
        def TerminateProcess(handle: int, exit_code: int) -> bool:
            terminated.append((handle, exit_code))
            return True

    monkeypatch.setattr(processes, "_WINDOWS", True)
    monkeypatch.setattr(processes, "_open_windows_process", lambda _access, _pid: 9)
    monkeypatch.setattr(processes, "_windows_kernel32", lambda: Kernel32())
    monkeypatch.setattr(
        processes, "_close_windows_handle", lambda handle: closed.append(handle)
    )

    processes.force_terminate_pid(1234)

    assert terminated == [(9, 1)]
    assert closed == [9]
