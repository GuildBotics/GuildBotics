"""GitHub as a provider: the code host and the Projects board."""

from __future__ import annotations

from typing import cast

from guildbotics.entities.team import Person, Service
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.github.github_utils import (
    GitHubAppAuth,
    get_agent_token,
    get_github_account_type,
    get_github_username,
)
from guildbotics.integrations.provider import (
    Provider,
    ProviderCheck,
    configured_check,
)
from guildbotics.runtime.context import Context
from guildbotics.utils.env_loader import workspace_secret_store

_SECTION = "github"
#: What each kind of GitHub account authenticates with.
_REQUIRED_KEYS = {
    GitHubAppAuth.GITHUB_APPS: (
        "github_installation_id",
        "github_app_id",
        "github_private_key",
    ),
    GitHubAppAuth.MACHINE_USER: ("github_access_token",),
}


def _verify(person: Person) -> list[ProviderCheck]:
    checks = []
    for key in _REQUIRED_KEYS.get(get_github_account_type(person), ()):
        if key in GITHUB.account_info_keys:
            configured = person.has_account_info(key)
            target = f"account_info.{key}"
        else:
            target = person.to_person_env_key(key)
            configured = (key != "github_private_key" and person.has_secret(key)) or (
                bool(workspace_secret_store().get(target))
            )
        checks.append(
            configured_check(
                _SECTION, "github_credential", person, key, target, configured
            )
        )
    return checks


def _credentialed(person: Person) -> bool:
    """A token in the environment, or a GitHub App (whose key is in the
    keychain)."""
    return any(
        person.has_secret(key) for key in GITHUB.secret_keys
    ) or person.has_account_info("github_app_id")


async def _diagnose(context: Context, members: list[Person]) -> list[ProviderCheck]:
    if context.team.project.get_service_name(Service.TICKET_MANAGER) != "github":
        return []
    checks: list[ProviderCheck] = []
    lane_checked = False
    for member in members:
        if member.person_type == "human":
            checks.append(await _human_member(context, member))
            continue
        c = context.clone_for(member)
        try:
            ticket_manager = cast(GitHubTicketManager, c.get_ticket_manager())
            statuses = await ticket_manager.get_statuses()
            checks.append(
                ProviderCheck(
                    section=_SECTION,
                    code="github_project_access",
                    status="ok",
                    message="GitHub project status options were fetched.",
                    person_id=member.person_id,
                    context={"status_count": len(statuses)},
                )
            )
            if not lane_checked:
                checks.extend(_lane_mapping(ticket_manager, statuses))
                lane_checked = True
            checks.append(await _agent_assignment(ticket_manager, member))
        except Exception as exc:
            checks.append(
                ProviderCheck(
                    section=_SECTION,
                    code="github_access",
                    status="error",
                    message=_safe_error("GitHub read-only check failed", exc),
                    person_id=member.person_id,
                    context={"error_type": type(exc).__name__},
                )
            )
        finally:
            await c.aclose()
    return checks


async def _human_member(context: Context, member: Person) -> ProviderCheck:
    username = get_github_username(member)
    if not username:
        return ProviderCheck(
            section=_SECTION,
            code="human_github_user",
            status="error",
            message="GitHub username is not configured for this human member.",
            person_id=member.person_id,
        )
    c = context.clone_for(member)
    try:
        ticket_manager = cast(GitHubTicketManager, c.get_ticket_manager())
        if await ticket_manager.is_assignable_user(username):
            return ProviderCheck(
                section=_SECTION,
                code="human_github_user",
                status="ok",
                message="GitHub username resolved to an assignable user account.",
                person_id=member.person_id,
                target=username,
            )
        return ProviderCheck(
            section=_SECTION,
            code="human_github_user",
            status="error",
            message="GitHub username could not be resolved as an assignable user account.",
            person_id=member.person_id,
            target=username,
        )
    except Exception as exc:
        return ProviderCheck(
            section=_SECTION,
            code="human_github_user",
            status="error",
            message=_safe_error("GitHub user account check failed", exc),
            person_id=member.person_id,
            target=username,
            context={"error_type": type(exc).__name__},
        )
    finally:
        await c.aclose()


def _lane_mapping(
    ticket_manager: GitHubTicketManager, statuses: list[str]
) -> list[ProviderCheck]:
    """Validate that the configured ready/done lanes exist on the board.

    The working lane is optional: a missing working lane is reported as a
    warning (tickets simply are not moved on start), never an error.
    """
    status_set = set(statuses)
    lane_map = ticket_manager.lane_map
    ready = lane_map.get(GitHubTicketManager.LANE_READY)
    done = lane_map.get(GitHubTicketManager.LANE_DONE)
    working = lane_map.get(GitHubTicketManager.LANE_WORKING)

    checks: list[ProviderCheck] = []
    missing = [name for name in (ready, done) if name and name not in status_set]
    if missing:
        checks.append(
            ProviderCheck(
                section=_SECTION,
                code="github_lane_missing",
                status="error",
                message="Required workflow lanes are missing from the GitHub Project "
                f"status options: {', '.join(missing)}.",
                context={"missing": missing, "available": sorted(status_set)},
            )
        )
    else:
        checks.append(
            ProviderCheck(
                section=_SECTION,
                code="github_lane_mapping",
                status="ok",
                message="Ready and done lanes exist in the GitHub Project.",
                context={"ready": ready, "done": done},
            )
        )
    if working and working not in status_set:
        checks.append(
            ProviderCheck(
                section=_SECTION,
                code="github_working_lane_missing",
                status="warning",
                message=f"Configured working lane '{working}' is not a GitHub Project "
                "status; tickets will not be moved to a working lane on start.",
                context={"working": working},
            )
        )
    return checks


async def _agent_assignment(
    ticket_manager: GitHubTicketManager, member: Person
) -> ProviderCheck:
    """Verify each member can receive ticket assignments.

    A member whose GitHub username resolves to a real user account needs
    nothing else. Otherwise the remediation depends on the member type: human
    members are assigned through GitHub assignees, so a human whose username
    does not resolve has a misconfigured username (the ``Agent`` field does
    not apply to them). Non-human identities (GitHub Apps, machine users) are
    assigned through the project's ``Agent`` field and need a matching option.
    """
    username = get_github_username(member)
    if username and await ticket_manager.is_assignable_user(username):
        return ProviderCheck(
            section=_SECTION,
            code="github_agent_assignment",
            status="ok",
            message="Member resolves to a GitHub user account; the Agent field is not required.",
            person_id=member.person_id,
            context={"github_username": username},
        )

    if get_github_account_type(member) in ("", GitHubAppAuth.HUMAN):
        return ProviderCheck(
            section=_SECTION,
            code="github_member_not_assignable",
            status="error",
            message="Member's GitHub username could not be resolved to a user "
            "account; check the configured GitHub username.",
            person_id=member.person_id,
            context={"github_username": username},
        )

    token = get_agent_token(member)
    options = await ticket_manager.get_agent_field_options()
    if token in options:
        return ProviderCheck(
            section=_SECTION,
            code="github_agent_assignment",
            status="ok",
            message="Member is assigned through the project's Agent field option.",
            person_id=member.person_id,
            context={"agent_option": token},
        )
    return ProviderCheck(
        section=_SECTION,
        code="github_agent_field_required",
        status="error",
        message="Member does not resolve to a GitHub user and has no Agent field "
        "option; set the Agent field for this member.",
        person_id=member.person_id,
        context={"agent_option": token},
    )


def _safe_error(prefix: str, exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {message}"


GITHUB = Provider(
    name="github",
    secret_keys=frozenset({"GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_KEY"}),
    account_info_keys=frozenset(
        {
            "github_username",
            "github_account_type",
            "github_app_id",
            "github_installation_id",
        }
    ),
    code_hosting=lambda logger, person, team: GitHubCodeHostingService(person, team),
    ticket_manager=GitHubTicketManager,
    credentialed=_credentialed,
    verify=_verify,
    diagnose=_diagnose,
)
