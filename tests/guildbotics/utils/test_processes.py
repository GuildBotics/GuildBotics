from __future__ import annotations

import errno
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from guildbotics.utils import processes


def test_native_process_lifecycle() -> None:
    """Exercise real process handles, including their ctypes types on Windows."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert processes.pid_exists(child.pid)
        processes.force_terminate_pid(child.pid)
        assert child.wait(timeout=10) != 0
        assert not processes.pid_exists(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_launching_app_name_walks_parents_and_reads_bundle_name(
    monkeypatch, tmp_path, fake_platform
):
    bundle = tmp_path / "Visual Studio Code.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleDisplayName": "Visual Studio Code"})
    )
    fake_platform(processes, "darwin")
    monkeypatch.setattr(processes.os, "getpid", lambda: 42)
    # The fake `ps` answers as macOS would, in POSIX spelling.
    rows = {"42": "41 /usr/bin/python", "41": f"1 {contents.as_posix()}/MacOS/Electron"}
    calls = []

    def ps(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=rows[argv[2]])

    monkeypatch.setattr(processes.subprocess, "run", ps)
    assert processes.launching_app_name() == "Visual Studio Code"
    assert calls == [
        ["ps", "-p", "42", "-o", "ppid=,comm="],
        ["ps", "-p", "41", "-o", "ppid=,comm="],
    ]


@pytest.mark.parametrize("output", ["", "42 /usr/bin/python", "1 /usr/bin/python"])
def test_launching_app_name_omits_unavailable_app(monkeypatch, output, fake_platform):
    fake_platform(processes, "darwin")
    monkeypatch.setattr(processes.os, "getpid", lambda: 42)
    monkeypatch.setattr(
        processes.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output)
    )
    assert processes.launching_app_name() == ""


def test_launching_app_name_does_not_probe_other_platforms(monkeypatch, fake_platform):
    fake_platform(processes, "linux")
    monkeypatch.setattr(
        processes.subprocess, "run", lambda *a, **k: pytest.fail("must not run ps")
    )
    assert processes.launching_app_name() == ""


def test_pid_exists_posix_handles_missing_and_permission_denied(monkeypatch) -> None:

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


def test_force_terminate_pid_posix_uses_sigkill(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(processes.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, signal_number: calls.append((pid, signal_number)),
    )

    processes.force_terminate_pid(1234)

    assert calls == [(1234, 9)]
