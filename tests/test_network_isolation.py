"""Exercise the suite-wide network guard in a separate pytest process."""

import os
import socket
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest


def _run(test_path: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            "pyproject.toml",
            str(test_path),
            "-q",
            "-rA",
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
    _write_lifecycle_probe(tmp_path)
    result = _run(tmp_path, env={**os.environ, "GUILDBOTICS_TEST_FIXTURE_DNS": "1"})
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.INTERRUPTED, output
    for host in (
        "192.0.2.1",
        "example.com",
        "collection.example",
        "fixture.example",
        "late.example",
    ):
        assert host in output
    assert "External network attempts" in output
    assert "ERROR test_lifecycle.py::test_session_fixture" in output
    assert "PASSED test_lifecycle.py::test_bare_marker_is_rejected" in output
    for name in (
        "module_fixture",
        "external_address",
        "external_name",
        "external_udp",
        "external_legacy_dns",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        assert f"ERROR test_network_probe.py::test_{name}" in output
    if hasattr(socket.socket, "sendmsg"):
        assert "ERROR test_network_probe.py::test_external_sendmsg" in output
    for name in ("ipv4_loopback", "ipv6_loopback", "localhost"):
        node = f"test_network_probe.py::test_{name}"
        assert f"PASSED {node}" in output
        assert f"ERROR {node}" not in output


def _write_lifecycle_probe(tmp_path: Path) -> Path:
    (tmp_path / "conftest.py").write_text(
        """
import os
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
    if os.environ.get("GUILDBOTICS_TEST_FIXTURE_DNS") == "1":
        session.exitstatus = pytest.ExitCode.INTERRUPTED
""",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_lifecycle.py"
    test_file.write_text(
        """
import os
import socket
import pytest
from tests import conftest

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


def test_bare_marker_is_rejected(request):
    request.node.add_marker(pytest.mark.real_device)
    try:
        with pytest.raises(pytest.UsageError, match="real_device requires a known opt-in flag"):
            conftest.pytest_collection_modifyitems(request.config, [request.node])
    finally:
        request.node.own_markers.pop()
""",
        encoding="utf-8",
    )
    return test_file


def test_network_guard_covers_collection_and_session_in_xdist(tmp_path: Path) -> None:
    test_file = _write_lifecycle_probe(tmp_path)
    parallel = _run(
        test_file,
        "-n",
        "2",
        env={**os.environ, "GUILDBOTICS_TEST_FIXTURE_DNS": "0"},
    )
    assert parallel.returncode != 0, parallel.stdout + parallel.stderr
    assert "2 passed" in parallel.stdout, parallel.stdout + parallel.stderr
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


def test_real_device_requires_explicit_opt_in(tmp_path: Path) -> None:
    test_file = tmp_path / "test_real_device.py"
    test_file.write_text(
        """
import pytest
from tests import conftest


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("contract", marks=pytest.mark.real_device("GUILDBOTICS_CONTRACT_PROBE")),
        pytest.param("environment", marks=pytest.mark.real_device("GUILDBOTICS_E2E_ENVIRONMENT_FROM")),
    ],
)
def test_real_device(request, kind):
    guard = request.config.stash[conftest._NETWORK_GUARD]
    assert guard.exempt
    assert request.node.get_closest_marker("timeout").args == (1800,)
    guard._check("example.com")
        """,
        encoding="utf-8",
    )
    env = {**os.environ}
    env.pop("CI", None)
    report = tmp_path / "opt-in.xml"
    for contract, environment, enabled, skipped in (
        ("1", "", "contract", "environment"),
        ("0", "/ready/workspace", "environment", "contract"),
    ):
        result = _run(
            test_file,
            f"--junitxml={report}",
            env={
                **env,
                "GUILDBOTICS_CONTRACT_PROBE": contract,
                "GUILDBOTICS_E2E_ENVIRONMENT_FROM": environment,
            },
        )
        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.OK, output
        skipped_by_name = {
            case.attrib["name"]: case.find("skipped") is not None
            for case in ElementTree.parse(report).iter("testcase")
        }
        assert skipped_by_name == {
            f"test_real_device[{enabled}]": False,
            f"test_real_device[{skipped}]": True,
        }

    env.update(
        GUILDBOTICS_CONTRACT_PROBE="1",
        GUILDBOTICS_E2E_ENVIRONMENT_FROM="/ready/workspace",
    )
    ci = _run(test_file, env={**env, "CI": "true"})
    assert ci.returncode == pytest.ExitCode.USAGE_ERROR
    assert "real_device tests cannot run in CI" in ci.stdout + ci.stderr

    parallel = _run(test_file, "-n", "2", env=env)
    assert parallel.returncode == pytest.ExitCode.USAGE_ERROR
    assert (
        "real_device tests must run without pytest-xdist"
        in parallel.stdout + parallel.stderr
    )
