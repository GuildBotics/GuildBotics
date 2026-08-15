import contextlib
import logging
import os

import pytest

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Role
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT


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
    monkeypatch.setenv("HOME", str(home))
    # ``Path.home()`` reads USERPROFILE on Windows and HOME everywhere else.
    monkeypatch.setenv("USERPROFILE", str(home))


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
