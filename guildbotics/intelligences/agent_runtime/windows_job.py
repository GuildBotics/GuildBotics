"""Windows Job Object ownership for AI CLI subprocess trees."""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = (
    int(vars(subprocess)["CREATE_NO_WINDOW"]) if os.name == "nt" else 0x08000000
)
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_RESUME_FAILED = 0xFFFFFFFF


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ThreadID", ctypes.c_ulong),
        ("th32OwnerProcessID", ctypes.c_ulong),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
    ]


def creation_flags() -> int:
    """Return flags that keep a Windows agent hidden and initially suspended."""
    return _CREATE_SUSPENDED | _CREATE_NO_WINDOW


def _kernel32() -> Any:
    return vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)


def _api(name: str, argtypes: list[object], restype: object) -> Any:
    function = getattr(_kernel32(), name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _last_error() -> int:
    return int(vars(ctypes)["get_last_error"]())


def _error(operation: str) -> OSError:
    error = _last_error()
    return OSError(error, f"{operation} failed with Windows error {error}")


def _create_job_handle() -> int:
    api = _api(
        "CreateJobObjectW",
        [ctypes.c_void_p, ctypes.c_wchar_p],
        ctypes.c_void_p,
    )
    handle = int(api(None, None) or 0)
    if not handle:
        raise _error("CreateJobObjectW")
    return handle


def _configure_job(handle: int) -> None:
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    api = _api(
        "SetInformationJobObject",
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong],
        ctypes.c_int,
    )
    if not api(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise _error("SetInformationJobObject")


def _open_process_for_job(pid: int) -> int:
    api = _api(
        "OpenProcess",
        [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong],
        ctypes.c_void_p,
    )
    handle = int(api(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid) or 0)
    if not handle:
        raise _error("OpenProcess")
    return handle


def _assign_process(job_handle: int, process_handle: int) -> None:
    api = _api(
        "AssignProcessToJobObject",
        [ctypes.c_void_p, ctypes.c_void_p],
        ctypes.c_int,
    )
    if not api(job_handle, process_handle):
        raise _error("AssignProcessToJobObject")


def _thread_ids_for(pid: int) -> list[int]:
    snapshot_api = _api(
        "CreateToolhelp32Snapshot",
        [ctypes.c_ulong, ctypes.c_ulong],
        ctypes.c_void_p,
    )
    snapshot = int(snapshot_api(_TH32CS_SNAPTHREAD, 0) or 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        raise _error("CreateToolhelp32Snapshot")
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        thread_ids: list[int] = []
        first = _api(
            "Thread32First",
            [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)],
            ctypes.c_int,
        )
        next_thread = _api(
            "Thread32Next",
            [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)],
            ctypes.c_int,
        )
        has_entry = bool(first(snapshot, ctypes.byref(entry)))
        while has_entry:
            if int(entry.th32OwnerProcessID) == pid:
                thread_ids.append(int(entry.th32ThreadID))
            has_entry = bool(next_thread(snapshot, ctypes.byref(entry)))
        return thread_ids
    finally:
        _close_handle(snapshot)


def _resume_thread(thread_id: int) -> None:
    api = _api(
        "OpenThread",
        [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong],
        ctypes.c_void_p,
    )
    handle = int(api(_THREAD_SUSPEND_RESUME, False, thread_id) or 0)
    if not handle:
        raise _error("OpenThread")
    try:
        resume = _api(
            "ResumeThread",
            [ctypes.c_void_p],
            ctypes.c_ulong,
        )
        if int(resume(handle)) == _RESUME_FAILED:
            raise _error("ResumeThread")
    finally:
        _close_handle(handle)


def _terminate_job(handle: int) -> None:
    api = _api(
        "TerminateJobObject",
        [ctypes.c_void_p, ctypes.c_uint],
        ctypes.c_int,
    )
    if not api(handle, 1):
        raise _error("TerminateJobObject")


def _close_handle(handle: int) -> None:
    if handle:
        _api("CloseHandle", [ctypes.c_void_p], ctypes.c_int)(handle)


@dataclass
class WindowsJob:
    """One owned Job Object configured to kill all members when closed."""

    handle: int

    @classmethod
    def create(cls) -> WindowsJob:
        handle = _create_job_handle()
        try:
            _configure_job(handle)
        except Exception:
            _close_handle(handle)
            raise
        return cls(handle)

    def assign_and_resume(self, pid: int) -> None:
        process_handle = _open_process_for_job(pid)
        try:
            _assign_process(self.handle, process_handle)
        finally:
            _close_handle(process_handle)
        thread_ids = _thread_ids_for(pid)
        if len(thread_ids) != 1:
            raise RuntimeError(
                f"Suspended process {pid} has {len(thread_ids)} primary thread candidates."
            )
        _resume_thread(thread_ids[0])

    def terminate(self) -> None:
        if self.handle:
            _terminate_job(self.handle)

    def close(self) -> None:
        handle, self.handle = self.handle, 0
        _close_handle(handle)


_JOBS: dict[int, WindowsJob] = {}
_JOBS_LOCK = threading.Lock()
_CLEANUP_TASKS: set[asyncio.Task[None]] = set()


def register_process_job(
    process: asyncio.subprocess.Process,
    job: WindowsJob,
) -> None:
    key = id(process)
    with _JOBS_LOCK:
        _JOBS[key] = job
    task = asyncio.create_task(_close_job_when_process_exits(process, key, job))
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


async def _close_job_when_process_exits(
    process: asyncio.subprocess.Process,
    key: int,
    job: WindowsJob,
) -> None:
    try:
        await process.wait()
    finally:
        with _JOBS_LOCK:
            if _JOBS.get(key) is job:
                _JOBS.pop(key, None)
        job.close()


def terminate_process_job(process: asyncio.subprocess.Process) -> bool:
    """Terminate and release the Job Object owned by ``process`` if present."""
    with _JOBS_LOCK:
        job = _JOBS.pop(id(process), None)
    if job is None:
        return False
    try:
        job.terminate()
    finally:
        job.close()
    return True
