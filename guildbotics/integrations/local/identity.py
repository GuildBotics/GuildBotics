"""Public local account names, deliberately different from person ids."""

from guildbotics.entities import Person
from guildbotics.integrations.member_identity import MemberIdentity


def username(person_id: str) -> str:
    """The local service name of a member id."""
    return f"local-{person_id}"


def member_identity(person: Person) -> MemberIdentity:
    name = username(person.person_id)
    return MemberIdentity((name,), f"@{name}")
