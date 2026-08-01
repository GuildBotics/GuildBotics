import pytest

from guildbotics.commands.errors import PersonExecutionNotAllowedError
from guildbotics.entities.team import Person, Project, Team
from guildbotics.runtime import member_context as member_context_module
from guildbotics.runtime.member_context import (
    ensure_execution_subject,
    resolve_member_context,
)


class FakeContext:
    def __init__(self, team, person=None):
        self.team = team
        self.person = person

    def clone_for(self, person):
        return FakeContext(self.team, person)


def _use_team(monkeypatch, *members):
    team = Team(project=Project(name="demo"), members=list(members))

    class FakeEdition:
        def get_context(self):
            return FakeContext(team)

    monkeypatch.setattr(member_context_module, "get_edition", lambda: FakeEdition())


def test_ensure_execution_subject_accepts_agent():
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    assert ensure_execution_subject(person) is person


def test_ensure_execution_subject_rejects_human():
    person = Person(person_id="hana", name="Hana", person_type="human")

    with pytest.raises(PersonExecutionNotAllowedError) as excinfo:
        ensure_execution_subject(person)

    assert excinfo.value.person_id == "hana"


def test_resolve_member_context_rejects_human_member(monkeypatch):
    _use_team(
        monkeypatch,
        Person(person_id="aiko", name="Aiko", person_type="agent"),
        Person(person_id="hana", name="Hana", person_type="human"),
    )

    with pytest.raises(PersonExecutionNotAllowedError):
        resolve_member_context("hana")


def test_resolve_member_context_clones_for_agent_member(monkeypatch):
    _use_team(
        monkeypatch,
        Person(person_id="aiko", name="Aiko", person_type="agent"),
    )

    context, person = resolve_member_context("Aiko")

    assert person.person_id == "aiko"
    assert context.person is person
