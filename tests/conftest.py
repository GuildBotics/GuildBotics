import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Role
from guildbotics.runtime.member_invocation import RUN_ENV, TASK_RUN_ENV
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import set_language
from guildbotics.utils.import_utils import ClassResolver
from tests.git_seed import WorkerGitSeed
from tests.windows_shards import (
    WINDOWS_SHARDS,
    verify_windows_shards,
    windows_shard_for_nodeid,
)

_PHASE_DURATION_OUTPUT: Path | None = None
_PHASE_DURATIONS: list[dict[str, object]] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--phase-durations-json",
        metavar="PATH",
        help="Write setup/call/teardown durations as machine-readable JSON.",
    )
    parser.addoption(
        "--windows-shard",
        choices=WINDOWS_SHARDS,
        help="Run exactly one duration-balanced Windows test shard.",
    )
    parser.addoption(
        "--verify-windows-shards",
        action="store_true",
        help="Verify that every collected node ID belongs to exactly one shard.",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _PHASE_DURATION_OUTPUT
    if hasattr(config, "workerinput"):
        return
    value = config.getoption("phase_durations_json")
    _PHASE_DURATION_OUTPUT = Path(value) if value else None
    _PHASE_DURATIONS.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
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


def pytest_sessionfinish(session: pytest.Session) -> None:
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


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    shard = config.getoption("windows_shard")
    if shard is None:
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (
            selected if windows_shard_for_nodeid(item.nodeid) == shard else deselected
        ).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


def pytest_collection_finish(session: pytest.Session) -> None:
    if not session.config.getoption("verify_windows_shards"):
        return
    counts = verify_windows_shards([item.nodeid for item in session.items])
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "Windows shard coverage: "
            + ", ".join(f"{shard}={counts[shard]}" for shard in WINDOWS_SHARDS)
        )


@pytest.fixture(scope="session")
def worker_git_seed(tmp_path_factory: pytest.TempPathFactory):
    """Build invariant Git history once in each xdist worker."""
    seed = WorkerGitSeed.create(tmp_path_factory.mktemp("git-seed"))
    yield seed
    seed.assert_unchanged()


@pytest.fixture(autouse=True)
def _isolate_workspace_data(monkeypatch, tmp_path):
    """Keep runtime files created by tests inside each test's temporary tree."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_machine_home(monkeypatch, tmp_path):
    """Keep ``~/.guildbotics`` out of the developer's real home directory.

    Device identity, the service lock, and a hub all live under the home
    directory rather than under a workspace, so a test that touches any of them
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
def host_time_zone(monkeypatch) -> str:
    """Give every agent environment the same host time zone, whatever the
    machine the suite runs on is set to.

    The host's zone is part of every environment's variables, so without this
    an assertion on them would pass on one developer's machine and fail on
    another's. Returns the zone, for the tests that state it.
    """
    from guildbotics.intelligences.agent_environment import spec

    monkeypatch.setattr(spec, "reload_localzone", lambda: None)
    monkeypatch.setattr(spec, "get_localzone_name", lambda: "Asia/Tokyo")
    return "Asia/Tokyo"


@pytest.fixture(autouse=True)
def _ignore_ambient_workflow_run(monkeypatch):
    """Keep tests off the workflow execution path of the member CLI guard.

    When the suite itself runs inside a GuildBotics workflow / member run,
    these variables are inherited from the environment. Tests that verify
    the workflow path set them explicitly via ``monkeypatch.setenv``.
    """
    monkeypatch.delenv(TASK_RUN_ENV, raising=False)
    monkeypatch.delenv(RUN_ENV, raising=False)


@pytest.fixture(autouse=True)
def _ignore_ambient_slack_tokens(monkeypatch):
    """Keep tests off real Slack tokens configured on the development machine.

    Secret resolution prefers real environment variables, so ambient global or
    ``<PERSON>_``-prefixed ``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN`` values
    would win over the values tests stage in the workspace secret store.
    """
    for name in list(os.environ):
        if name.endswith(("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _isolate_secret_key_registry():
    """Keep the process-lifetime secret-key provenance registry test-local.

    The registry is deliberately monotonic in production, so without this
    reset a key registered by one test would stay secret for the whole suite.
    """
    from guildbotics.utils import secret_store

    secret_store._KNOWN_SECRET_ENV_KEYS.clear()
    yield
    secret_store._KNOWN_SECRET_ENV_KEYS.clear()


@pytest.fixture(autouse=True)
def english_locale():
    """Start every test in English, whatever the previous test switched to.

    The i18n locale is process-global; a test that sets it to ``ja`` and
    stops would leave every later sentence rendered in Japanese.
    """
    set_language("en")
    yield


@pytest.fixture(autouse=True)
def fake_keyring():
    """Keep tests off the developer's real OS keychain."""
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class InMemoryKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self):
            super().__init__()
            self.passwords: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self.passwords.get((service, username))

        def set_password(self, service, username, password):
            self.passwords[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.passwords:
                raise PasswordDeleteError(username)
            del self.passwords[(service, username)]

    backend = InMemoryKeyring()
    original = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


class _PlatformView:
    """``sys`` as one module sees it when told it runs on another platform.

    Everything but ``platform`` reads through to the real module, and whatever
    a test sets on the view stays on the view.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def __getattr__(self, name: str) -> Any:
        return getattr(sys, name)


@pytest.fixture
def fake_platform(monkeypatch) -> Callable[[ModuleType, str], Any]:
    """Make one module under test believe it runs on ``platform``.

    ``module.sys`` is the interpreter's own ``sys``, so setting ``platform`` on
    it fakes the platform for every import that happens while the test runs.
    A library imported for the first time in that window takes the branch for
    a platform it is not on -- ``mcp.server.stdio`` imports ``fcntl`` on
    Windows -- and whether the test passes then depends on which tests the
    same worker ran first. Only the module under test gets the view here; the
    returned view also takes any other ``sys`` attribute the test needs to set.
    """

    def fake(module: ModuleType, platform: str) -> Any:
        view = _PlatformView(platform)
        monkeypatch.setattr(module, "sys", view)
        return view

    return fake


@pytest.fixture
def posix_permissions() -> None:
    """Skip on a device whose file system has no POSIX permission bits.

    Windows keeps no owner/group/other bits, and ``chmod`` there only toggles
    the read-only attribute, so an assertion on ``0o600`` describes the test's
    own platform rather than the code under test.
    """
    if os.name == "nt":
        pytest.skip("Windows has no POSIX permission bits.")


@pytest.fixture
def symlinks(tmp_path) -> None:
    """Skip on a device where this session may not create a symbolic link.

    Windows grants that privilege to an elevated session or to one running
    with Developer Mode enabled, and CI runs elevated. A developer without it
    would otherwise read a privilege error as a failure of the code.
    """
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
    supply them: Windows and a bare Linux both need them installed separately.
    A device without them gets the ``CommandError`` its own test covers, and
    only a device with them can render anything.
    """
    import importlib

    try:
        importlib.import_module("weasyprint")
    except (ImportError, OSError) as exc:
        pytest.skip(f"WeasyPrint's native libraries are not available: {exc}")


class FakeProject:
    """
    Fake project for testing, returns English language code and name.
    """

    def get_language_code(self) -> str:
        return "en"

    def get_language_name(self) -> str:
        return "English"


class FakeContext:
    """
    Fake context that holds team, person, task, logger, and a brain registry.
    """

    def __init__(self):
        # Minimal team and project for intelligences.functions
        self.team = type("T", (), {"project": FakeProject()})()
        # Create a person with "dev" and "pm" roles and account_info
        self.person = Person(
            person_id="p1",
            name="Tester",
            roles={
                "dev": Role(id="dev", summary="Developer", description="Writes code"),
                "pm": Role(id="pm", summary="PM", description="Manages project"),
            },
        )
        # Add account_info to person
        self.person.account_info = {
            "git_user": "Test User",
            "git_email": "test@example.com",
        }
        # Default task with id and repository
        self.task = Task(title="T", description="D")
        self.task.id = "task-123"
        self.task.repository = "test-repo"
        self.logger = logging.getLogger("test.context")
        # Registry for fake brains
        self._brains: dict[str, FakeBrain] = {}

    def get_brain(
        self, name: str, config: dict | None, class_resolver: ClassResolver | None
    ):
        return self._brains[name]


class FakeBrain:
    """
    Fake brain that returns a preset result and optional response_class.
    """

    def __init__(self, result, response_class=None):
        self._result = result
        self.response_class = response_class

    async def run(self, **kwargs):
        return self._result


@pytest.fixture
def fake_context() -> FakeContext:
    """
    Provides a FakeContext for tests.
    """
    return FakeContext()


@pytest.fixture
def stub_brain(fake_context):
    """
    Provides a helper to register a FakeBrain in the fake_context.

    Usage:
        stub_brain(name: str, result, response_class=None)
    """

    def _stub(name: str, result, response_class=None):
        fake_context._brains[name] = FakeBrain(result, response_class)

    return _stub


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


@pytest.fixture(autouse=True)
def _local_hub_transport(monkeypatch):
    """Run Hub tests without a real Desktop; macOS transport tests opt in explicitly."""
    from guildbotics.hub import secret_transport

    monkeypatch.setattr(secret_transport, "DELEGATES_TO_DESKTOP", False)
