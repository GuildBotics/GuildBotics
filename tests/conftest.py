import contextlib
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime.wire import (
    COMMAND_ENV,
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    MEMBER_BROKER_TOKEN_ENV,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_IDENTITY_ROOT,
    GUILDBOTICS_WORKSPACE_ROOT,
)
from guildbotics.utils.i18n_tool import set_language

_PHASE_DURATION_OUTPUT: Path | None = None
_PHASE_DURATIONS: list[dict[str, object]] = []
_NETWORK_AUDIT: list[tuple[str, str]] = []
_REAL_DEVICE_ENABLED = pytest.StashKey[frozenset[str]]()
_NETWORK_GUARD = pytest.StashKey["_NetworkGuard"]()
REAL_DEVICE_OPT_INS = frozenset(
    {
        "GUILDBOTICS_CONTRACT_PROBE",
        "GUILDBOTICS_GROK_SMOKE",
        "GUILDBOTICS_E2E_ENVIRONMENT_FROM",
    }
)
#: What a command's isolated environment is booted with and lends a turn's
#: provider, apart from the workspace root every test sets for itself.
COMMAND_ENVIRONMENT_VARIABLES = frozenset(
    {
        HOST_URL_ENV,
        HOST_TOKEN_ENV,
        COMMAND_ENV,
        GUILDBOTICS_CONFIG_DIR,
        GUILDBOTICS_IDENTITY_ROOT,
        MEMBER_BROKER_TOKEN_ENV,
    }
)
_NETWORK_ENV = {
    **{
        name: "http://127.0.0.1:1"
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
    },
    "NO_PROXY": "localhost,127.0.0.1,::1",
    "no_proxy": "localhost,127.0.0.1,::1",
    "GIT_ALLOW_PROTOCOL": "file",
}


class _NetworkGuard:
    """Guard collection and test execution through session finish."""

    def __init__(self, config: pytest.Config) -> None:
        self.audit = config.getoption("network_audit")
        self.patch = pytest.MonkeyPatch()
        self.original_env = {name: os.environ.get(name) for name in _NETWORK_ENV}
        self.item: pytest.Item | None = None
        self.exempt = False
        self.attempts: dict[str, set[str]] = {}
        self.lock = threading.Lock()
        self._install()
        self._child_process_guard(True)

    def _child_process_guard(self, enabled: bool) -> None:
        if self.audit:
            return
        for name, value in _NETWORK_ENV.items():
            original = self.original_env[name]
            if enabled:
                os.environ[name] = value
            elif original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original

    def _check(self, host: object) -> None:
        if host in (None, "localhost", "127.0.0.1", "::1"):
            return
        with self.lock:
            if self.exempt:
                return
            nodeid = self.item.nodeid if self.item is not None else "<outside test>"
            self.attempts.setdefault(nodeid, set()).add(str(host))
        if not self.audit:
            raise OSError(f"External network access from a test: {host}")

    def _install(self) -> None:
        connect = socket.socket.connect
        connect_ex = socket.socket.connect_ex
        sendto = socket.socket.sendto
        getaddrinfo = socket.getaddrinfo
        gethostbyname = socket.gethostbyname
        gethostbyname_ex = socket.gethostbyname_ex
        gethostbyaddr = socket.gethostbyaddr
        getnameinfo = socket.getnameinfo

        def guarded_connect(sock: socket.socket, address: object) -> None:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                self._check(address[0])  # type: ignore[index]
            connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: object) -> int:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                self._check(address[0])  # type: ignore[index]
            return connect_ex(sock, address)

        def guarded_sendto(sock: socket.socket, data: bytes, *args: Any) -> int:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                self._check(args[-1][0])
            return sendto(sock, data, *args)

        def guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any):
            self._check(host)
            return getaddrinfo(host, *args, **kwargs)

        def guarded_gethostbyname(host: str) -> str:
            self._check(host)
            return gethostbyname(host)

        def guarded_gethostbyname_ex(host: str):
            self._check(host)
            return gethostbyname_ex(host)

        def guarded_gethostbyaddr(host: str):
            self._check(host)
            return gethostbyaddr(host)

        def guarded_getnameinfo(sockaddr: tuple, flags: int):
            self._check(sockaddr[0])
            return getnameinfo(sockaddr, flags)

        self.patch.setattr(socket.socket, "connect", guarded_connect)
        self.patch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        self.patch.setattr(socket.socket, "sendto", guarded_sendto)
        self.patch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
        self.patch.setattr(socket, "gethostbyname", guarded_gethostbyname)
        self.patch.setattr(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
        self.patch.setattr(socket, "gethostbyaddr", guarded_gethostbyaddr)
        self.patch.setattr(socket, "getnameinfo", guarded_getnameinfo)
        if hasattr(socket.socket, "sendmsg"):
            sendmsg = socket.socket.sendmsg

            def guarded_sendmsg(sock: socket.socket, *args: Any, **kwargs: Any):
                address = kwargs.get("address", args[3] if len(args) > 3 else None)
                if address is not None and sock.family in (
                    socket.AF_INET,
                    socket.AF_INET6,
                ):
                    self._check(address[0])
                return sendmsg(sock, *args, **kwargs)

            self.patch.setattr(socket.socket, "sendmsg", guarded_sendmsg)

    def activate(self, item: pytest.Item, *, exempt: bool) -> None:
        with self.lock:
            self.item = item
            self.exempt = exempt
        self._child_process_guard(not exempt)

    def deactivate(self) -> None:
        with self.lock:
            self.item = None
            self.exempt = False
        self._child_process_guard(True)

    def finish_item(self, item: pytest.Item) -> list[str]:
        with self.lock:
            return sorted(self.attempts.pop(item.nodeid, set()))

    def remaining(self) -> list[tuple[str, str]]:
        with self.lock:
            return sorted(
                (nodeid, host)
                for nodeid, hosts in self.attempts.items()
                for host in hosts
            )

    def close(self) -> None:
        self.patch.undo()
        self._child_process_guard(False)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--phase-durations-json",
        metavar="PATH",
        help="Write setup/call/teardown durations as machine-readable JSON.",
    )
    parser.addoption(
        "--network-audit",
        action="store_true",
        help="Report Python socket attempts without blocking them.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    global _PHASE_DURATION_OUTPUT
    config.stash[_REAL_DEVICE_ENABLED] = frozenset(
        flag
        for flag in REAL_DEVICE_OPT_INS
        if (
            bool(os.environ.get(flag))
            if flag == "GUILDBOTICS_E2E_ENVIRONMENT_FROM"
            else os.environ.get(flag) == "1"
        )
    )
    if config.stash[_REAL_DEVICE_ENABLED]:
        if os.environ.get("CI"):
            raise pytest.UsageError("real_device tests cannot run in CI")
        if config.getoption("numprocesses", default=None) not in (None, 0, "0"):
            raise pytest.UsageError("real_device tests must run without pytest-xdist")
    if hasattr(config, "workerinput"):
        return
    value = config.getoption("phase_durations_json")
    _PHASE_DURATION_OUTPUT = Path(value) if value else None
    _PHASE_DURATIONS.clear()
    _NETWORK_AUDIT.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[_NETWORK_GUARD] = _NetworkGuard(session.config)


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_NETWORK_GUARD, None)
    if guard is not None:
        guard.close()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "teardown":
        for key, value in report.user_properties:
            if key == "network_attempt":
                _NETWORK_AUDIT.append((report.nodeid, value))
    if _PHASE_DURATION_OUTPUT is None or report.when not in {
        "setup",
        "call",
        "teardown",
    }:
        return
    _PHASE_DURATIONS.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "duration_seconds": round(report.duration, 6),
        }
    )


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session):
    yield
    guard = session.config.stash.get(_NETWORK_GUARD, None)
    pending = guard.remaining() if guard is not None else []
    if hasattr(session.config, "workeroutput"):
        session.config.workeroutput["network_attempts"] = pending
    else:
        _NETWORK_AUDIT.extend(pending)
    if (_NETWORK_AUDIT or pending) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if not hasattr(session.config, "workeroutput") and _NETWORK_AUDIT:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "External network attempts")
            for nodeid, target in sorted(_NETWORK_AUDIT):
                reporter.write_line(f"{nodeid}: {target}")
            reporter.write_line(f"Total: {len(_NETWORK_AUDIT)}")
    if _PHASE_DURATION_OUTPUT is None:
        return
    _PHASE_DURATION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _PHASE_DURATION_OUTPUT.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exit_status": int(session.exitstatus),
                "phases": sorted(
                    _PHASE_DURATIONS,
                    key=lambda item: (str(item["nodeid"]), str(item["phase"])),
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object | None) -> None:
    for nodeid, host in getattr(node, "workeroutput", {}).get("network_attempts", []):
        _NETWORK_AUDIT.append((f"{node.gateway.id}:{nodeid}", host))


def _real_device_flag(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker("real_device")
    if marker is None:
        return None
    if (
        len(marker.args) != 1
        or marker.kwargs
        or not isinstance(marker.args[0], str)
        or marker.args[0] not in REAL_DEVICE_OPT_INS
    ):
        raise pytest.UsageError("real_device requires a known opt-in flag")
    return marker.args[0]


def _real_device_enabled(item: pytest.Item) -> bool:
    """Whether ``item`` is a real-device test this run opted in to: one that
    runs on this machine as it is, network and workspace alike."""
    return _real_device_flag(item) in item.config.stash[_REAL_DEVICE_ENABLED]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    enabled = config.stash[_REAL_DEVICE_ENABLED]
    for item in items:
        flag = _real_device_flag(item)
        if flag is not None and flag not in enabled:
            value = (
                "a workspace whose isolated environment is ready on this device"
                if flag == "GUILDBOTICS_E2E_ENVIRONMENT_FROM"
                else "1"
            )
            item.add_marker(
                pytest.mark.skip(reason=f"Set {flag}={value} to run this test.")
            )
        if flag is not None and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(30 * 60))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    guard = item.config.stash[_NETWORK_GUARD]
    guard.activate(item, exempt=_real_device_enabled(item))
    try:
        yield
    finally:
        guard.deactivate()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.when != "teardown":
        return
    guard = item.config.stash[_NETWORK_GUARD]
    hosts = guard.finish_item(item)
    for host in hosts:
        report.user_properties.append(("network_attempt", host))
    if hosts and not guard.audit:
        message = f"External network attempts: {', '.join(hosts)}"
        if report.failed:
            report.sections.append(("network attempts", message))
        else:
            report.outcome = "failed"
            report.longrepr = message
    # Product threads must end after each test, before HOME/workspace changes.
    # Module/session fixtures cannot keep them alive between tests.
    threads = sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("guildbotics-")
    )
    if threads:
        message = (
            f"{item.nodeid} left GuildBotics threads running: {', '.join(threads)}"
        )
        if report.failed:
            report.sections.append(("remaining GuildBotics threads", message))
        else:
            report.outcome = "failed"
            report.longrepr = message


@pytest.fixture(autouse=True)
def _isolate_workspace_data(monkeypatch, tmp_path):
    """Keep runtime files created by tests inside each test's temporary tree."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_machine_home(monkeypatch, tmp_path):
    """Keep ``~/.guildbotics`` out of the developer's real home directory.

    Device identity and the service lock live under the home directory rather
    than under a workspace, so a test that touches any of them
    would otherwise write into the machine the suite is running on -- and read
    back whatever that machine already had.
    """
    # Not created here: many tests create this same directory themselves, and
    # writers make their own parents anyway.
    home = tmp_path / "home"
    # ``Path.home()`` reads USERPROFILE on Windows and HOME everywhere else, so
    # both are set wherever a test points the home directory somewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _ignore_ambient_command_environment(request, monkeypatch):
    """Keep tests off the variables of a command they are run inside.

    An agent runs the suite inside its command's isolated environment, which
    inherits them: the window to the host would carry each test's inference
    out to the real host, and the configuration directory would point at the
    real workspace's instead of the test's own. A real-device test opted in
    to keeps them: the workspace it is pointed at is the one it checks.
    """
    if _real_device_enabled(request.node):
        return
    for name in COMMAND_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_inference(monkeypatch):
    """Start every test with no way to the inference APIs installed.

    The process installs one when it builds a context, and it stays for the
    process; without this reset a test that builds one would decide what a
    later test's brains reach.
    """
    from guildbotics.intelligences.brains import inference

    monkeypatch.setattr(inference, "_installed", None)


@pytest.fixture(autouse=True)
def english_locale():
    """Start every test in English, whatever the previous test switched to.

    The i18n locale is process-global; a test that sets it to ``ja`` and
    stops would leave every later sentence rendered in Japanese.
    """
    set_language("en")
    yield


def _restore_logger_handlers(
    logger: logging.Logger, before: list[logging.Handler]
) -> None:
    """Put ``logger``'s handlers back to the objects in ``before``.

    ``logging.Handler`` has no equality, so membership is identity. Handlers
    the test attached are dropped; handlers it removed are attached again.
    """
    extras = [handler for handler in logger.handlers if handler not in before]
    missing = [handler for handler in before if handler not in logger.handlers]
    for handler in extras:
        logger.removeHandler(handler)
    for handler in missing:
        logger.addHandler(handler)


@pytest.fixture(autouse=True)
def _restore_guildbotics_log_handlers():
    """Restore handlers on the process-wide logger after each test."""
    logger = logging.getLogger("guildbotics")
    before = list(logger.handlers)
    yield
    _restore_logger_handlers(logger, before)


@pytest.fixture
def symlinks(tmp_path) -> None:
    """Skip on a device where this session may not create a symbolic link."""
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except OSError as exc:
        pytest.skip(f"This session cannot create a symbolic link: {exc}")
    probe.unlink()


@pytest.fixture
def weasyprint_libraries() -> None:
    """Skip where WeasyPrint's GTK libraries are not installed on the device.

    They are native rather than Python, so the dependency resolver cannot
    supply them: a bare Linux needs them installed separately.
    A device without them gets the ``CommandError`` its own test covers, and
    only a device with them can render anything.
    """
    import importlib

    try:
        importlib.import_module("weasyprint")
    except (ImportError, OSError) as exc:
        pytest.skip(f"WeasyPrint's native libraries are not available: {exc}")


@contextlib.contextmanager
def coverage_suspended():
    cov = None
    try:
        import coverage

        cov = coverage.Coverage.current()
    except Exception:
        cov = None

    if cov is not None:
        cov.stop()
    try:
        yield
    finally:
        if cov is not None:
            cov.start()
