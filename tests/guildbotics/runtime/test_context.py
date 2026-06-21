"""Unit tests for `guildbotics.runtime.context.Context`.

This suite verifies:
- `get_ticket_manager` caching behavior.
- `clone_for` independence for person and cache.
- `update_task` replaces the active task.
- `get_brain` delegates to the provided `BrainFactory` with language code.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.chat_service import ChatIdentity
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.brains.brain import Brain
from guildbotics.loader.team_loader import TeamLoader
from guildbotics.runtime.brain_factory import BrainFactory
from guildbotics.runtime.context import Context
from guildbotics.runtime.integration_factory import IntegrationFactory
from guildbotics.runtime.loader_factory import LoaderFactory
from guildbotics.utils.import_utils import ClassResolver

# ---- Test doubles ---------------------------------------------------------------------------


class DummyTeamLoader(TeamLoader):
    """Simple TeamLoader stub returning a provided Team instance."""

    def __init__(self, team: Team):
        self._team = team

    def load(self) -> Team:
        """Return the configured Team instance."""
        return self._team


class DummyLoaderFactory(LoaderFactory):
    """LoaderFactory stub returning the provided Team via DummyTeamLoader."""

    def __init__(self, team: Team):
        self._team = team
        self.create_team_loader_calls = 0

    def create_team_loader(self) -> TeamLoader:
        """Return a TeamLoader that loads the configured Team."""
        self.create_team_loader_calls += 1
        return DummyTeamLoader(self._team)


class DummyTicketManager(TicketManager):
    """Concrete TicketManager test double with no-op async methods."""

    async def get_task_to_work_on(self) -> Task | None:
        """No-op fetch task."""
        return None

    async def move_ticket(self, task: Task, new_status: str) -> bool:
        """No-op move ticket."""
        return False

    async def add_comment_to_ticket(self, task: Task, comment: str) -> None:
        """No-op add comment."""
        return None

    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        """No-op get URL."""
        return ""

    async def update_ticket(self, task: Task) -> None:
        """No-op update ticket."""
        return None


class DummyIntegrationFactory(IntegrationFactory):
    """IntegrationFactory stub capturing calls and returning DummyTicketManager."""

    def __init__(self):
        self.ticket_manager_calls: list[tuple[Person, Team]] = []
        self.chat_service_calls: list[tuple[Person, Team]] = []

    def create_ticket_manager(
        self, logger: logging.Logger, person: Person, team: Team
    ) -> TicketManager:
        """Return a new DummyTicketManager and record the call."""
        self.ticket_manager_calls.append((person, team))
        return DummyTicketManager(logger, person, team)

    def create_chat_service(self, logger: logging.Logger, person: Person, team: Team):
        """Record the call and return a minimal chat service stub."""
        self.chat_service_calls.append((person, team))

        class _DummyChatService:
            def __init__(self) -> None:
                self.closed = False

            async def get_bot_identity(self):
                return ChatIdentity(user_id="U_TEST", display_name="dummy")

            async def aclose(self):
                self.closed = True

        return _DummyChatService()


class DummyBrain(Brain):
    """Concrete Brain stub capturing inputs; does nothing on run()."""

    async def run(self, message: str, **kwargs: Any):
        """No-op run implementation for abstract base."""
        return {"message": message, **kwargs}


class DummyBrainFactory(BrainFactory):
    """BrainFactory stub recording `create_brain` calls and returning DummyBrain."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, logging.Logger]] = []

    def create_brain(
        self,
        person_id: str,
        name: str,
        language_code: str,
        logger: logging.Logger,
        config: dict | None = None,
        class_resolver: ClassResolver | None = None,
    ) -> Brain:
        """Record arguments and return a DummyBrain with the given name/logger."""
        self.calls.append((person_id, name, language_code, logger))
        return DummyBrain(person_id=person_id, name=name, logger=logger)


def _make_team(language: str = "en") -> Team:
    """Create a minimal Team with a Project configured for the given language."""
    project = Project(
        name="demo",
        language=language,
        services={},
    )
    return Team(project=project, members=[])


# ---- Tests -----------------------------------------------------------------------------------


def test_get_ticket_manager_is_cached(monkeypatch):
    """Context.get_ticket_manager returns a cached instance and uses factory once."""
    team = _make_team(language="en")
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    logger = logging.getLogger("test")

    person = Person(person_id="p1", name="Tester")
    task = Task(title="T", description="D")

    ctx = Context(
        loader_factory=loader_factory,
        integration_factory=integration_factory,
        brain_factory=brain_factory,
        logger=logger,
        person=person,
        task=task,
        message="Initial message",
    )

    tm1 = ctx.get_ticket_manager()
    tm2 = ctx.get_ticket_manager()

    assert tm1 is tm2, "TicketManager should be cached per Context instance"
    assert len(integration_factory.ticket_manager_calls) == 1, "Factory called once"


def test_clone_for_independence_person_and_cache():
    """clone_for yields a new Context with independent caches and person."""
    team = _make_team(language="en")
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    logger = logging.getLogger("test")

    person1 = Person(person_id="p1", name="A")
    person2 = Person(person_id="p2", name="B")
    task = Task(title="T", description="D")

    ctx1 = Context(
        loader_factory=loader_factory,
        integration_factory=integration_factory,
        brain_factory=brain_factory,
        logger=logger,
        person=person1,
        task=task,
        message="Initial message",
    )

    # Prime cache in original context to ensure clone has its own cache
    _ = ctx1.get_ticket_manager()

    ctx2 = ctx1.clone_for(person2)

    assert ctx2 is not ctx1
    assert ctx2.person is person2
    assert ctx1.person is person1
    # Cached ticket manager must not be carried over
    assert ctx2.ticket_manager is None
    # Message is copied
    assert ctx2.pipe == "Initial message"


@pytest.mark.asyncio
async def test_context_aclose_closes_cached_chat_resources():
    team = _make_team(language="en")
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    logger = logging.getLogger("test")

    person = Person(person_id="p1", name="Tester")
    task = Task(title="T", description="D")
    ctx = Context(
        loader_factory=loader_factory,
        integration_factory=integration_factory,
        brain_factory=brain_factory,
        logger=logger,
        person=person,
        task=task,
        message="Initial message",
    )

    chat_service = ctx.get_chat_service()
    await ctx.aclose()

    assert getattr(chat_service, "closed", False) is True
    assert ctx.chat_service is None


def test_update_task_replaces_task():
    """update_task should replace the current task without changing cached resources."""
    team = _make_team(language="en")
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    logger = logging.getLogger("test")

    person = Person(person_id="p1", name="Tester")
    task1 = Task(title="T1", description="D1")
    ctx = Context(
        loader_factory=loader_factory,
        integration_factory=integration_factory,
        brain_factory=brain_factory,
        logger=logger,
        person=person,
        task=task1,
        message="Initial message",
    )
    ticket_manager = ctx.get_ticket_manager()

    task2 = Task(title="T2", description="D2")
    ctx.update_task(task2)
    assert ctx.task is task2
    assert ctx.get_ticket_manager() is ticket_manager


def test_get_brain_delegates_to_factory_with_language():
    """get_brain delegates to factory and passes derived language code."""
    # Project language code 'ja' should be passed through to BrainFactory
    team = _make_team(language="ja")
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    logger = logging.getLogger("test")

    person = Person(person_id="p1", name="Tester")
    task = Task(title="T", description="D")

    ctx = Context(
        loader_factory=loader_factory,
        integration_factory=integration_factory,
        brain_factory=brain_factory,
        logger=logger,
        person=person,
        task=task,
        message="Initial message",
    )

    brain = ctx.get_brain("planner", None, None)

    # Returned instance is from factory
    assert isinstance(brain, DummyBrain)
    # Validate delegation parameters captured by the factory
    assert brain_factory.calls, "Factory should have been called"
    person_id, name, lang, lg = brain_factory.calls[-1]
    assert person_id == "p1"
    assert name == "planner"
    assert lang == "ja"
    assert lg is logger
