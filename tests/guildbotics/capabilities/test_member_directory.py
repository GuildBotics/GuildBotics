"""Member context publishes public names for teammates, including humans."""

import pytest

from guildbotics.capabilities.member_context import member_context
from guildbotics.entities import Person
from guildbotics.integrations.factory import PROVIDERS
from tests.guildbotics.local_code_host import local_member


@pytest.mark.asyncio
async def test_context_roster_includes_every_member_and_configured_service():
    context = local_member()
    context.team.members.append(
        Person(person_id="owner", name="Owner", person_type="human")
    )
    result = await member_context(context)
    assert result["roster"] == [
        {
            "person_id": member.person_id,
            "name": member.name,
            "person_type": member.person_type,
            "services": {
                "local": {
                    "names": list(PROVIDERS["local"].member_identity(member).names),
                    "mention": PROVIDERS["local"].member_identity(member).mention,
                }
            },
        }
        for member in context.team.members
    ]
    assert "local-owner" in result["roster"][1]["services"]["local"]["names"]


@pytest.mark.parametrize("login", ["Review-App[bot]", "review-app"])
def test_github_directory_resolves_rest_and_graphql_bot_names(login):
    member = Person(
        person_id="reviewer", name="Reviewer", account_info={"github_username": login}
    )
    identity = PROVIDERS["github"].member_identity(member)
    assert identity.matches("REVIEW-APP[bot]")
    assert identity.matches("Review-App")
    assert not identity.matches("another-app")
    assert identity.mention == "⚙reviewer"
