import pytest

from guildbotics.commands.errors import PersonExecutionNotAllowedError
from guildbotics.entities.team import Person
from guildbotics.runtime.member_context import ensure_execution_subject


def test_ensure_execution_subject_accepts_agent():
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    assert ensure_execution_subject(person) is person


def test_ensure_execution_subject_rejects_human():
    person = Person(person_id="hana", name="Hana", person_type="human")

    with pytest.raises(PersonExecutionNotAllowedError) as excinfo:
        ensure_execution_subject(person)

    assert excinfo.value.person_id == "hana"
