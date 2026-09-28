"""Exercise the suite-wide network guard in a separate pytest process."""

import subprocess
import sys
from pathlib import Path


def test_network_isolation(tmp_path: Path) -> None:
    test_file = tmp_path / "test_network_probe.py"
    test_file.write_text(
        """
import socket


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
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            str(test_file),
            "-q",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "7 passed, 4 errors" in result.stdout, result.stdout + result.stderr
    assert "192.0.2.1" in result.stdout
    assert "example.com" in result.stdout
    assert "External network attempts" in result.stdout
