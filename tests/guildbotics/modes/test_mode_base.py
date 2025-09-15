import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Repository, Role, Service, Team
from guildbotics.intelligences.common import AgentResponse
from guildbotics.modes.mode_base import ModeBase


def make_team(
    services: dict[str, dict[str, str | dict[str, str]]] | None = None,
) -> Team:
    """Helper to create a minimal Team with given project services."""
    project = Project(
        language="en",
        repositories=[Repository(name="r", description="", is_default=True)],
        services=services or {},
    )
    person = Person(
        person_id="p1",
        name="Tester",
        roles={"dev": Role(id="dev", summary="Developer", description="Writes code")},
    )
    return Team(project=project, members=[person])


def test_to_mode_cls_name_converts_snake_case_to_full_path():
    assert ModeBase._to_mode_cls_name("edit") == "guildbotics.modes.edit_mode.EditMode"
    assert (
        ModeBase._to_mode_cls_name("ticket")
        == "guildbotics.modes.ticket_mode.TicketMode"
    )


def test_to_mode_cls_name_returns_default_comment_when_none():
    assert (
        ModeBase._to_mode_cls_name(None) == "guildbotics.modes.comment_mode.CommentMode"
    )


def test_to_mode_cls_name_returns_input_when_already_dotted():
    dotted = "guildbotics.modes.comment_mode.CommentMode"
    assert ModeBase._to_mode_cls_name(dotted) == dotted


def test_get_mode_uses_instantiate_class(monkeypatch):
    # Arrange a minimal context stub
    class Ctx:
        task = Task(title="T", description="D", role="dev", mode="edit")

    captured = {}

    def fake_instantiate(module_and_cls, expected_type=None, **kwargs):
        captured["module_and_cls"] = module_and_cls
        captured["expected_type"] = expected_type
        captured["kwargs"] = kwargs
        return "INSTANCE"

    # Patch the symbol imported in mode_base
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.instantiate_class", fake_instantiate
    )

    # Act
    inst = ModeBase.get_mode(Ctx())

    # Assert
    assert inst == "INSTANCE"
    assert captured["module_and_cls"] == "guildbotics.modes.edit_mode.EditMode"
    assert captured["expected_type"] is ModeBase
    assert "context" in captured["kwargs"]
    assert captured["kwargs"]["context"].task.mode == "edit"


def test_get_use_case_description_if_available_with_load_class_mock(monkeypatch):
    # Create a dummy Mode class with specific dependencies
    class DummyMode(ModeBase):
        def __init__(self, *_, **__):  # pragma: no cover - not instantiated
            pass

        async def run(
            self, messages: list[Message]
        ) -> AgentResponse:  # pragma: no cover
            return AgentResponse(status=AgentResponse.ASKING, message="")

        @classmethod
        def get_dependent_services(cls) -> list[Service]:
            return [Service.CODE_HOSTING_SERVICE]

        @classmethod
        def get_use_case_description(cls) -> str:
            return "dummy description"

    # Patch load_class used in mode_base to return our DummyMode irrespective of input
    monkeypatch.setattr("guildbotics.modes.mode_base.load_class", lambda _: DummyMode)

    # When required service is missing ⇒ not available (None)
    desc = ModeBase.get_use_case_description_if_available("edit", [])
    assert desc is None

    # When required service is present ⇒ returns description
    desc = ModeBase.get_use_case_description_if_available(
        "edit", [Service.CODE_HOSTING_SERVICE]
    )
    assert desc == "dummy description"


def test_get_available_modes_filters_by_services():
    # No services ⇒ only comment mode should be available
    team = make_team(services={})
    labels = ModeBase.get_available_modes(team)
    assert "comment" in labels
    # conservative check: dependent modes not available
    assert "edit" not in labels and "ticket" not in labels

    # Only code hosting ⇒ edit available, ticket not
    team = make_team(
        services={
            Service.CODE_HOSTING_SERVICE.value: {"name": "github"},
        }
    )
    labels = ModeBase.get_available_modes(team)
    assert "edit" in labels
    assert "ticket" not in labels

    # Both code hosting and ticket manager ⇒ both available
    team = make_team(
        services={
            Service.CODE_HOSTING_SERVICE.value: {"name": "github"},
            Service.TICKET_MANAGER.value: {"name": "github_issues"},
        }
    )
    labels = ModeBase.get_available_modes(team)
    assert "edit" in labels and "ticket" in labels and "comment" in labels


@pytest.mark.asyncio
async def test_get_done_response_uses_talk_as_and_formats_output(monkeypatch):
    # Create a minimal concrete Mode implementation for testing
    class SimpleMode(ModeBase):
        async def run(
            self, messages: list[Message]
        ) -> AgentResponse:  # pragma: no cover
            return AgentResponse(status=AgentResponse.ASKING, message="")

        @classmethod
        def get_dependent_services(cls) -> list[Service]:  # pragma: no cover
            return []

        @classmethod
        def get_use_case_description(cls) -> str:  # pragma: no cover
            return ""

    # Stub talk_as to avoid LLM calls
    async def fake_talk_as(context, topic, context_location, conversation_history):
        return "Summary text from LLM"

    monkeypatch.setattr("guildbotics.modes.mode_base.talk_as", fake_talk_as)

    # Mock get_workspace_path to return a fake path
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path",
        lambda person_id: "/tmp/fake_workspace",
    )

    # Minimal context stub: only fields accessed by talk_as via session_state
    class FakeCodeHostingService:
        pass

    class Ctx:
        def __init__(self):
            self.person = Person(person_id="p1", name="Tester")
            self.person.account_info = {
                "git_user": "Test User",
                "git_email": "test@example.com",
            }
            self.task = Task(title="T", description="D")
            self.task.id = "test-123"
            self.task.repository = "test-repo"
            self.active_role = Role(id="dev", summary="", description="")

        def get_code_hosting_service(self, repository=None):
            return FakeCodeHostingService()

    mode = SimpleMode(context=Ctx())
    messages = [Message(content="Hi", author="u", author_type=Message.USER)]

    resp = await mode.get_done_response(
        title="Feature PR",
        url="https://example.com/pr/1",
        messages=messages,
        topic="Done topic",
        context_location="Ticket Comment",
    )

    assert resp.status == AgentResponse.DONE
    assert "Summary text from LLM" in resp.message
    assert f"{Task.OUTPUT_PREFIX}[Feature PR](https://example.com/pr/1)" in resp.message
