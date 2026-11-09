"""Process inspection and forced termination."""

from __future__ import annotations

import errno
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path


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


def pid_exists(pid: int) -> bool:
    """Return whether ``pid`` exists without altering the target process."""
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def force_terminate_pid(pid: int) -> None:
    """Immediately terminate ``pid`` using the native platform primitive."""
    os.kill(pid, signal.SIGKILL)
