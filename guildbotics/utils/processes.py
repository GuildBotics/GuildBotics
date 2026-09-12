"""Cross-platform process inspection and forced termination."""

from __future__ import annotations

import ctypes
import errno
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

_WINDOWS = os.name == "nt"
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def launching_app_name() -> str:
    """Return the nearest macOS parent app's bundle name, when observable."""
    if sys.platform != "darwin":
        return ""
    pid = os.getpid()
    seen: set[int] = set()
    try:
        while pid > 1 and pid not in seen:
            seen.add(pid)
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                capture_output=True,
                text=True,
                check=True,
                timeout=1,
            )
            parent, executable = result.stdout.strip().split(maxsplit=1)
            app, marker, _ = executable.partition(".app/Contents/MacOS/")
            if marker:
                bundle = Path(app + ".app")
                with (bundle / "Contents" / "Info.plist").open("rb") as stream:
                    info = plistlib.load(stream)
                return str(
                    info.get("CFBundleDisplayName")
                    or info.get("CFBundleName")
                    or bundle.stem
                )
            pid = int(parent)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        plistlib.InvalidFileException,
    ):
        pass
    return ""


def _windows_kernel32() -> Any:
    return vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)


def _windows_last_error() -> int:
    return int(vars(ctypes)["get_last_error"]())


def _open_windows_process(access: int, pid: int) -> int:
    open_process = _windows_kernel32().OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    handle = open_process(access, False, pid)
    return int(handle or 0)


def _close_windows_handle(handle: int) -> None:
    close_handle = _windows_kernel32().CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)


def _windows_process_is_active(handle: int) -> bool:
    exit_code = ctypes.c_ulong()
    get_exit_code = _windows_kernel32().GetExitCodeProcess
    get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    get_exit_code.restype = ctypes.c_int
    if not get_exit_code(handle, ctypes.byref(exit_code)):
        error = _windows_last_error()
        raise OSError(error, "GetExitCodeProcess failed")
    return exit_code.value == _STILL_ACTIVE


def pid_exists(pid: int) -> bool:
    """Return whether ``pid`` exists without altering the target process."""
    if pid <= 0:
        return False
    if _WINDOWS:
        handle = _open_windows_process(_PROCESS_QUERY_LIMITED_INFORMATION, pid)
        if handle:
            try:
                return _windows_process_is_active(handle)
            finally:
                _close_windows_handle(handle)
        return _windows_last_error() == _ERROR_ACCESS_DENIED

    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def terminate_posix_process_group(pid: int, *, force: bool = False) -> None:
    """Send the requested termination signal to a POSIX process group."""
    signal_name = "SIGKILL" if force else "SIGTERM"
    vars(os)["killpg"](pid, vars(signal)[signal_name])


def force_terminate_pid(pid: int) -> None:
    """Immediately terminate ``pid`` using the native platform primitive."""
    if _WINDOWS:
        handle = _open_windows_process(_PROCESS_TERMINATE, pid)
        if not handle:
            error = _windows_last_error()
            raise OSError(error, f"OpenProcess failed for PID {pid}")
        try:
            terminate = _windows_kernel32().TerminateProcess
            terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            terminate.restype = ctypes.c_int
            if not terminate(handle, 1):
                error = _windows_last_error()
                raise OSError(error, f"TerminateProcess failed for PID {pid}")
        finally:
            _close_windows_handle(handle)
        return
    os.kill(pid, vars(signal)["SIGKILL"])
