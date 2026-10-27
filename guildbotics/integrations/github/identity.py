"""GitHub's credential-free names, including its two bot login spellings."""

from guildbotics.entities import Person
from guildbotics.integrations.member_identity import MemberIdentity


def normalize_login(login: str) -> str:
    """The name shared by REST and GraphQL (which omits the bot suffix)."""
    return login.casefold().removesuffix("[bot]")


def member_identity(person: Person) -> MemberIdentity:
    login = str(person.account_info.get("github_username") or "").strip()
    name = normalize_login(login)
    token = f"⚙{person.person_id}"
    names = tuple(dict.fromkeys((name, name + "[bot]", token))) if name else (token,)
    # Agent field assignments use the same public token as mentions.
    return MemberIdentity(names, token)
