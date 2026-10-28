"""Branvil's credential-free member directory."""

from guildbotics.entities import Person
from guildbotics.integrations.member_identity import MemberIdentity


def member_identity(person: Person) -> MemberIdentity:
    name = str(person.account_info.get("branvil_username") or "").strip()
    return MemberIdentity((name.casefold(),), f"@{name}") if name else MemberIdentity()
