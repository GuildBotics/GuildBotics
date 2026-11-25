"""Exercise the suite-wide thread guard in separate pytest processes."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("workers", [0, 2])
def test_thread_guard_reports_each_leak_after_fixture_finalizers(
    tmp_path: Path, workers: int
) -> None:
    test_file = tmp_path / "test_threads.py"
    test_file.write_text(
        """
import threading
import pytest


def start(name):
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, name=name, daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    return close


phase_cleanup = []


@pytest.fixture(autouse=True)
def clear_previous_leak():
    # Leave this test's thread up through the guard's teardown report.
    while phase_cleanup:
        phase_cleanup.pop()()


def leak():
    phase_cleanup.append(start("guildbotics-phase"))


@pytest.fixture
def resource(request):
    if request.param == "setup":
        leak()
        raise RuntimeError("setup failure")
    yield
    if request.param == "teardown":
        leak()
        raise RuntimeError("teardown failure")


@pytest.mark.parametrize("resource", ["setup", "call", "teardown"], indirect=True)
def test_leaves_thread(resource, request):
    if request.node.callspec.params["resource"] == "call":
        leak()


@pytest.fixture
def cleaned():
    close = start("guildbotics-cleaned")
    yield
    close()


def test_cleaned_thread(cleaned):
    pass


@pytest.fixture(scope="module")
def module_thread():
    close = start("guildbotics-module")
    yield
    close()


@pytest.fixture(scope="session")
def session_thread():
    close = start("guildbotics-session")
    yield
    close()


def test_threads_retained_between_tests(module_thread, session_thread):
    pass


def test_scoped_finalizers_release_threads(module_thread, session_thread):
    pass
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            "pyproject.toml",
            "--rootdir",
            str(tmp_path),
            str(test_file),
            "-q",
            "-rA",
            "--tb=short",
            "--dist=loadfile",
            "-n",
            str(workers),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    errors = [line for line in output.splitlines() if line.startswith("ERROR ")]
    passed = [line for line in output.splitlines() if line.startswith("PASSED ")]
    for phase in ("setup", "call", "teardown"):
        node = f"test_threads.py::test_leaves_thread[{phase}]"
        assert any(node in line for line in errors), output
        assert f"{node} left GuildBotics threads running: guildbotics-phase" in output
        if phase != "call":
            assert f"{phase} failure" in output

    node = "test_threads.py::test_threads_retained_between_tests"
    assert any(node in line for line in errors), output
    assert (
        f"{node} left GuildBotics threads running: guildbotics-module, guildbotics-session"
        in output
    )
    for name in ("test_cleaned_thread", "test_scoped_finalizers_release_threads"):
        node = f"test_threads.py::{name}"
        assert any(node in line for line in passed), output
        assert all(node not in line for line in errors), output
