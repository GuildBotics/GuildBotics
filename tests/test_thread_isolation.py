"""Exercise the suite-wide thread guard in separate pytest processes."""

import subprocess
import sys
from pathlib import Path

import pytest


def _run(test_file: Path, workers: int):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            "pyproject.toml",
            "--rootdir",
            str(test_file.parent),
            str(test_file),
            "-q",
            "--tb=short",
            "-n",
            str(workers),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_guard_fails_the_test_that_left_a_thread(
    tmp_path: Path, workers: int, phase: str
) -> None:
    test_file = tmp_path / "test_leak.py"
    test_file.write_text(
        f"""
import threading
import pytest


def leak():
    # This intentional leak stays in this disposable process.
    threading.Thread(
        target=threading.Event().wait, name="guildbotics-probe", daemon=True
    ).start()


@pytest.fixture
def resource():
    if {phase!r} == "setup":
        leak()
        raise RuntimeError("setup failure")
    yield
    if {phase!r} == "teardown":
        leak()
        raise RuntimeError("teardown failure")


def test_leaves_thread(resource):
    if {phase!r} == "call":
        leak()
""",
        encoding="utf-8",
    )
    result = _run(test_file, workers)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_leak.py::test_leaves_thread left GuildBotics threads running" in output
    assert "guildbotics-probe" in output
    if phase != "call":
        assert f"{phase} failure" in output


@pytest.mark.parametrize("workers", [0, 2])
def test_guard_runs_after_all_fixture_finalizers(tmp_path: Path, workers: int) -> None:
    test_file = tmp_path / "test_cleanup.py"
    test_file.write_text(
        """
import threading
import pytest


@pytest.fixture(scope="session")
def resource():
    stop = threading.Event()
    thread = threading.Thread(
        target=stop.wait, name="guildbotics-cleaned", daemon=True
    )
    thread.start()
    yield
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_cleaned_thread(resource):
    pass
""",
        encoding="utf-8",
    )
    result = _run(test_file, workers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("scope", ["module", "session"])
def test_guard_rejects_threads_kept_between_tests(tmp_path: Path, scope: str) -> None:
    test_file = tmp_path / "test_scope.py"
    test_file.write_text(
        f"""
import threading
import pytest


@pytest.fixture(scope={scope!r})
def resource():
    stop = threading.Event()
    thread = threading.Thread(
        target=stop.wait, name="guildbotics-long-scope", daemon=True
    )
    thread.start()
    yield
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_first(resource):
    pass


def test_last(resource):
    pass
""",
        encoding="utf-8",
    )
    result = _run(test_file, 0)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_scope.py::test_first left GuildBotics threads running" in output
    assert "test_scope.py::test_last left GuildBotics threads running" not in output
    assert "2 passed, 1 error" in output
