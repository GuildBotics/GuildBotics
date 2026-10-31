"""Member context publishes public names for teammates, including humans."""

import pytest

from guildbotics.capabilities.member_context import member_context
from guildbotics.entities import Person
from guildbotics.integrations.factory import PROVIDERS
from tests.guildbotics.local_code_host import local_member


@pytest.mark.asyncio
@pytest.mark.parametrize("person_type", ["human", "agent"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "provider,account,names,mention",
    [
        (
            "branvil",
            {"branvil_username": "reviewer-bv"},
            ["reviewer-bv"],
            "@reviewer-bv",
        ),
        ("slack", {"slack_user_id": "U_REVIEWER"}, ["U_REVIEWER"], "<@U_REVIEWER>"),
        (
            "github",
            {"github_username": "reviewer-gh[bot]"},
            ["reviewer-gh", "reviewer-gh[bot]", "⚙reviewer"],
            "⚙reviewer",
        ),
        ("local", {}, ["local-reviewer"], "@local-reviewer"),
    ],
)
async def test_roster_only_exposes_usable_service_identities(
    person_type, configured, provider, account, names, mention
):
    context = local_member()
    member = Person(
        person_id="reviewer",
        name="Reviewer",
        person_type=person_type,
        account_info=account if configured else {},
    )
    context.team.members.append(member)
    kind = "chat_service" if provider == "slack" else "code_hosting_service"
    # The context's own identity read is the local fixture's; the roster asks
    # the configured descriptors without reaching any external service.
    context.team.project.services = {kind: {"name": provider}}
    result = await member_context(context)
    assert [entry["person_id"] for entry in result["roster"]] == ["aiko", "reviewer"]
    entry = result["roster"][1]
    assert (entry["name"], entry["person_type"]) == ("Reviewer", person_type)
    if not configured:
        if provider == "github":
            names = ["⚙reviewer"]
        elif provider != "local":
            assert entry["services"] == {}
            return
    assert entry["services"] == {provider: {"names": names, "mention": mention}}


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
