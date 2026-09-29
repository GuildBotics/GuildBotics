"""Exercise the suite-wide network guard in a separate pytest process."""

import os
import socket
import subprocess
import sys
from pathlib import Path


def _run(test_file: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            "pyproject.toml",
            str(test_file),
            "-q",
            "--tb=short",
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def test_network_isolation(tmp_path: Path) -> None:
    test_file = tmp_path / "test_network_probe.py"
    test_file.write_text(
        """
import socket
import pytest


@pytest.fixture(scope="module")
def module_dns():
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError:
        pass


def test_module_fixture(module_dns):
    pass


def test_external_address():
    with socket.socket() as sock:
        try:
            sock.connect_ex(("192.0.2.1", 443))
        except OSError:
            pass


def test_external_name():
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError:
        pass


def test_external_udp():
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        try:
            sock.sendto(b"test", ("192.0.2.1", 443))
        except OSError:
            pass


def test_external_legacy_dns():
    try:
        socket.gethostbyname("example.com")
    except OSError:
        pass


def test_gethostbyname_ex():
    try:
        socket.gethostbyname_ex("example.com")
    except OSError:
        pass


def test_gethostbyaddr():
    try:
        socket.gethostbyaddr("192.0.2.1")
    except OSError:
        pass


def test_getnameinfo():
    try:
        socket.getnameinfo(("192.0.2.1", 443), 0)
    except OSError:
        pass


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="no sendmsg")
def test_external_sendmsg():
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        try:
            sock.sendmsg([b"test"], [], 0, ("192.0.2.1", 443))
        except OSError:
            pass


def test_ipv4_loopback():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname()) as client:
            assert client.getpeername()[0] == "127.0.0.1"


def test_ipv6_loopback():
    assert socket.getaddrinfo("::1", 443, socket.AF_INET6)


def test_localhost():
    assert socket.getaddrinfo("localhost", 443)
""",
        encoding="utf-8",
    )
    result = _run(test_file)
    expected = (
        "12 passed, 9 errors"
        if hasattr(socket.socket, "sendmsg")
        else "11 passed, 1 skipped, 8 errors"
    )
    assert expected in result.stdout, result.stdout + result.stderr
    assert "192.0.2.1" in result.stdout
    assert "example.com" in result.stdout
    assert "External network attempts" in result.stdout


def test_network_guard_covers_collection_and_session(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        """
import socket
import threading
import pytest


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    def late_dns():
        try:
            socket.gethostbyname("late.example")
        except OSError:
            pass

    thread = threading.Thread(target=late_dns)
    thread.start()
    thread.join()
""",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_lifecycle.py"
    test_file.write_text(
        """
import os
import socket
import pytest

try:
    socket.getaddrinfo("collection.example", 443)
except OSError:
    pass


@pytest.fixture(scope="session")
def session_dns():
    if os.environ.get("GUILDBOTICS_TEST_FIXTURE_DNS") == "1":
        try:
            socket.gethostbyname_ex("fixture.example")
        except OSError:
            pass


def test_session_fixture(session_dns):
    pass
""",
        encoding="utf-8",
    )
    result = _run(test_file, env={**os.environ, "GUILDBOTICS_TEST_FIXTURE_DNS": "1"})
    assert result.returncode != 0
    for host in ("collection.example", "fixture.example", "late.example"):
        assert host in result.stdout, result.stdout + result.stderr
    assert "1 passed, 1 error" in result.stdout, result.stdout + result.stderr

    parallel = _run(
        test_file,
        "-n",
        "2",
        env={**os.environ, "GUILDBOTICS_TEST_FIXTURE_DNS": "0"},
    )
    assert parallel.returncode != 0, parallel.stdout + parallel.stderr
    assert "1 passed" in parallel.stdout, parallel.stdout + parallel.stderr
    assert "<outside test>: collection.example" in parallel.stdout
    assert "<outside test>: late.example" in parallel.stdout


def test_network_audit_reports_xdist_attempts(tmp_path: Path) -> None:
    test_file = tmp_path / "test_audit.py"
    test_file.write_text(
        """
from tests import conftest


def test_first(request):
    request.config.stash[conftest._NETWORK_GUARD]._check("192.0.2.1")


def test_second(request):
    request.config.stash[conftest._NETWORK_GUARD]._check("192.0.2.2")
""",
        encoding="utf-8",
    )
    result = _run(test_file, "-n", "2", "--network-audit")
    assert result.returncode != 0
    assert "2 passed" in result.stdout, result.stdout + result.stderr
    assert "192.0.2.1" in result.stdout
    assert "192.0.2.2" in result.stdout
    assert "Total: 2" in result.stdout


def test_network_attempt_preserves_existing_exit_status(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        """
import socket
import pytest


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    try:
        socket.gethostbyname("late.example")
    except OSError:
        pass
    session.exitstatus = pytest.ExitCode.INTERRUPTED
""",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_exit_status.py"
    test_file.write_text("def test_success(): pass\n", encoding="utf-8")
    result = _run(test_file)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "late.example" in result.stdout


def test_real_device_requires_explicit_opt_in(tmp_path: Path) -> None:
    test_file = tmp_path / "test_real_device.py"
    test_file.write_text(
        """
import pytest
from tests import conftest


@pytest.mark.real_device("GUILDBOTICS_CONTRACT_PROBE")
def test_real_device(request):
    guard = request.config.stash[conftest._NETWORK_GUARD]
    assert guard.exempt
    assert request.node.get_closest_marker("timeout").args == (1800,)
    guard._check("example.com")
""",
        encoding="utf-8",
    )
    env = {**os.environ, "GUILDBOTICS_CONTRACT_PROBE": "0"}
    env.pop("CI", None)
    skipped = _run(test_file, env=env)
    assert "1 skipped" in skipped.stdout, skipped.stdout + skipped.stderr

    env["GUILDBOTICS_CONTRACT_PROBE"] = "1"
    enabled = _run(test_file, env=env)
    assert "1 passed" in enabled.stdout, enabled.stdout + enabled.stderr

    ci = _run(test_file, env={**env, "CI": "true"})
    assert "real_device tests cannot run in CI" in ci.stdout + ci.stderr

    parallel = _run(test_file, "-n", "2", env=env)
    assert (
        "real_device tests must run without pytest-xdist"
        in parallel.stdout + parallel.stderr
    )


def test_real_device_marker_requires_a_known_flag(tmp_path: Path) -> None:
    test_file = tmp_path / "test_invalid_real_device.py"
    test_file.write_text(
        """
import pytest


@pytest.mark.real_device
def test_bare_marker():
    pass
""",
        encoding="utf-8",
    )
    result = _run(test_file)
    assert result.returncode != 0
    assert "real_device requires a known opt-in flag" in result.stdout + result.stderr
