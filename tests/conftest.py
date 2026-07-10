import contextlib
import logging

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Role
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.secret_store import ENV_FILE_BACKEND, SECRETS_BACKEND_ENV


@pytest.fixture(autouse=True)
def _force_env_file_secret_backend(monkeypatch):
    """Keep tests off the developer's real OS keychain.

    Keyring-path tests opt back in through the ``fake_keyring`` fixture,
    which removes this override and installs an in-memory backend.
    """
    monkeypatch.setenv(SECRETS_BACKEND_ENV, ENV_FILE_BACKEND)


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install an in-memory keyring backend and re-enable auto-detection."""
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
    monkeypatch.delenv(SECRETS_BACKEND_ENV, raising=False)
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
