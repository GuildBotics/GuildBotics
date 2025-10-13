import contextlib
import logging

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Role


class FakeProject:
    """
    Fake project for testing, returns English language code and name.
    """

    def get_language_code(self) -> str:
        return "en"

    def get_language_name(self) -> str:
        return "English"


class FakeCodeHostingService:
    """
    Fake code hosting service for testing.
    """

    pass


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
        self.task = Task(title="T", description="D", role="dev")
        self.task.id = "task-123"
        self.task.repository = "test-repo"
        self.active_role = self.person.get_role(self.task.role)
        self.logger = logging.getLogger("test.context")
        # Registry for fake brains
        self._brains: dict[str, FakeBrain] = {}

    def get_brain(self, name: str):
        return self._brains[name]

    def get_code_hosting_service(self, repository: str | None = None):
        """
        Get a fake code hosting service.
        """
        return FakeCodeHostingService()


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
