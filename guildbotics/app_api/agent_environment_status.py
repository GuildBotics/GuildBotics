"""What each member's AI CLI slots may reach on this device, and why not.

The same resolution decides three things the Desktop shows: the effective
permission preview, the status band that says a grant cannot be resolved
here, and the judgement offered while a grant is being added. It reads the
runtime's own view of the configuration (the per-member slot mapping and the
shared and local grants), so what it shows is what a turn would get. The
environment enforces every setting the same way on every device, so what
can keep a slot from starting here is a grant this device cannot resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from guildbotics.app_api.models import (
    AgentEnvironmentStatusResponse,
    EnvironmentAccessStatus,
    EnvironmentDenyStatus,
    EnvironmentGrantStatus,
    EnvironmentMemberStatus,
    EnvironmentProblem,
    EnvironmentSlotStatus,
    GrantEvaluation,
    GrantScope,
)
from guildbotics.intelligences.agent_environment.contract import (
    AccessContractError,
    DocumentGrant,
    LocalGrants,
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
    SharedGrants,
    load_local_grants,
    load_shared_grants,
    local_path_missing,
    redact_path,
    resolve_access,
    sensitive_grant_reason,
)
from guildbotics.intelligences.brains.cli_agent import get_cli_agent_mapping


def agent_environment_status(
    person_ids: list[str], *, platform: str = sys.platform
) -> AgentEnvironmentStatusResponse:
    """Resolve the grants and every slot of the given members against this device.

    Nothing is created here: a document directory that does not exist yet is
    reported absent, exactly what the turn would create when it starts.
    """
    home = Path.home()
    try:
        access = resolve_access(load_shared_grants(), load_local_grants(), create=False)
        problem = ""
    except AccessContractError as exc:
        access = ResolvedAccess()
        problem = str(exc)
    # What a turn would refuse to start over: the resolution failing as a
    # whole, or a local path this device does not have (shown on its row).
    problems = [problem] if problem else []
    problems += [
        local_path_missing(redact_path(g.path, home))
        for g in access.paths
        if not g.present
    ]
    members = [
        EnvironmentMemberStatus(
            person_id=person_id,
            slots=[
                _slot_status(slot, info.adapter, info.network, problems)
                for slot, info in sorted(get_cli_agent_mapping(person_id).items())
            ],
        )
        for person_id in person_ids
    ]
    return AgentEnvironmentStatusResponse(
        platform=platform,
        working_directory="<workspace>/.guildbotics/local/...",
        access=_access_status(access, problem, home),
        members=members,
    )


def _access_status(
    access: ResolvedAccess, problem: str, home: Path
) -> EnvironmentAccessStatus:
    def grant(g: ResolvedGrant) -> EnvironmentGrantStatus:
        return EnvironmentGrantStatus(
            path=redact_path(g.path, home),
            grant=g.grant,
            access=g.access,
            present=g.present,
        )

    return EnvironmentAccessStatus(
        documents=[grant(g) for g in access.documents],
        paths=[grant(g) for g in access.paths],
        denied=[
            EnvironmentDenyStatus(path=redact_path(d.path, home), builtin=d.builtin)
            for d in access.denied
        ],
        problem=problem,
    )


def _slot_status(
    slot: str, tool: str, network: NetworkPolicy, access_problems: list[str]
) -> EnvironmentSlotStatus:
    return EnvironmentSlotStatus(
        slot=slot,
        tool=tool,
        network=network,
        problems=[
            EnvironmentProblem(setting="grants", reason=reason)
            for reason in dict.fromkeys(access_problems)
        ],
    )


#: ``(person_id, slot, setting, reason)`` for one slot that cannot start here.
EnvironmentProblemEntry = tuple[str, str, str, str]


def agent_environment_problems(person_ids: list[str]) -> list[EnvironmentProblemEntry]:
    """Every slot that cannot start on this device, with the setting to open."""
    return [
        (member.person_id, slot.slot, problem.setting, problem.reason)
        for member in agent_environment_status(person_ids).members
        for slot in member.slots
        for problem in slot.problems
    ]


def evaluate_grant(
    scope: GrantScope, path: str, access: str = "read"
) -> GrantEvaluation:
    """Judge a grant before it is saved: a document, a local path, or a deny."""
    home = Path.home()
    if scope == "document":
        try:
            grant = DocumentGrant.model_validate({"path": path, "access": access})
        except ValidationError as exc:
            return _invalid(scope, path, access, _reason(exc))
        try:
            resolved = resolve_access(
                SharedGrants(documents=[grant]), LocalGrants(), home, create=False
            )
        except AccessContractError as exc:
            return _invalid(scope, path, access, str(exc))
        return GrantEvaluation(
            scope="document",
            path=path,
            access=access,
            valid=True,
            present=resolved.documents[0].present,
            sensitive=sensitive_grant_reason(path, home),
        )
    if scope == "deny":
        try:
            local = LocalGrants.model_validate({"deny": [path]})
        except ValidationError as exc:
            return _invalid(scope, path, "", _reason(exc))
        try:
            resolve_access(SharedGrants(), local, home)
        except AccessContractError as exc:
            return _invalid(scope, path, "", str(exc))
        return GrantEvaluation(scope="deny", path=path, valid=True, present=True)
    try:
        local = LocalGrants.model_validate(
            {"paths": [{"path": path, "access": access}]}
        )
    except ValidationError as exc:
        return _invalid("local", path, access, _reason(exc))
    try:
        resolve_access(SharedGrants(), local, home)
    except AccessContractError as exc:
        return _invalid("local", path, access, str(exc))
    return GrantEvaluation(
        scope="local",
        path=path,
        access=access,
        valid=True,
        present=True,
        sensitive=sensitive_grant_reason(path, home),
    )


def _invalid(scope: GrantScope, path: str, access: str, reason: str) -> GrantEvaluation:
    return GrantEvaluation(
        scope=scope, path=path, access=access, valid=False, reason=reason
    )


def _reason(exc: ValidationError) -> str:
    for line in str(exc).splitlines():
        if "Value error, " in line:
            return line.split("Value error, ", 1)[1].split(" [type=")[0]
    return str(exc).splitlines()[0]
