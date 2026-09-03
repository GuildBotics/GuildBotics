"""What each member's AI CLI slots may reach on this device, and why not.

The same resolution decides three things the Desktop shows: the effective
permission preview, the status band that says a setting cannot be enforced
here, and the judgement offered while a grant is being added. It reads the
runtime's own view of the configuration (the per-member slot mapping and the
shared and local grants), so what it shows is what a turn would get.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from guildbotics.app_api.models import (
    CliAgentNetworkSupportInfo,
    GrantEvaluation,
    GrantScope,
    SandboxAccessStatus,
    SandboxDenyStatus,
    SandboxExcludedStatus,
    SandboxGrantStatus,
    SandboxMemberStatus,
    SandboxProblem,
    SandboxSetting,
    SandboxSlotStatus,
    SandboxStatusResponse,
    SandboxTreeStatus,
)
from guildbotics.intelligences.brains.cli_agent import get_cli_agent_mapping
from guildbotics.intelligences.cli_agents import (
    CLI_AGENTS,
    get_cli_agent_search_path,
    unsupported_grant_reason,
    unsupported_network_reason,
)
from guildbotics.intelligences.sandbox import (
    DocumentGrant,
    LocalGrants,
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
    SandboxContractError,
    SharedGrants,
    load_local_grants,
    load_shared_grants,
    local_path_missing,
    redact_path,
    resolve_access,
    sensitive_grant_reason,
)


def network_support(
    platform: str = sys.platform,
) -> dict[str, CliAgentNetworkSupportInfo]:
    """The catalog's enforcement claims, for the editor's controls."""
    return {
        agent.name: CliAgentNetworkSupportInfo(
            command_modes=sorted(agent.network.command_modes_on(platform)),
            command_modes_anywhere=sorted(agent.network.command_modes_on(None)),
            web_modes=sorted(agent.network.web_modes),
            local_network_modes=sorted(agent.network.local_network_modes),
            grant_accesses=sorted(agent.network.grant_accesses),
            contract_applied=agent.network.contract_applied,
        )
        for agent in CLI_AGENTS
    }


def sandbox_status(
    person_ids: list[str], *, platform: str = sys.platform
) -> SandboxStatusResponse:
    """Resolve the grants and every slot of the given members against this device.

    Nothing is created here: a document directory that does not exist yet is
    reported absent, exactly what the turn would create when it starts.
    """
    home = Path.home()
    try:
        access = resolve_access(
            load_shared_grants(),
            load_local_grants(),
            get_cli_agent_search_path(),
            create=False,
        )
        problem = ""
    except SandboxContractError as exc:
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
        SandboxMemberStatus(
            person_id=person_id,
            slots=[
                _slot_status(
                    slot, info.adapter, info.network, access, problems, platform
                )
                for slot, info in sorted(get_cli_agent_mapping(person_id).items())
            ],
        )
        for person_id in person_ids
    ]
    return SandboxStatusResponse(
        platform=platform,
        working_directory="<workspace>/.guildbotics/local/...",
        access=_access_status(access, problem, home),
        members=members,
    )


def _access_status(
    access: ResolvedAccess, problem: str, home: Path
) -> SandboxAccessStatus:
    def grant(g: ResolvedGrant) -> SandboxGrantStatus:
        return SandboxGrantStatus(
            path=redact_path(g.path, home),
            grant=g.grant,
            access=g.access,
            present=g.present,
        )

    return SandboxAccessStatus(
        documents=[grant(g) for g in access.documents],
        paths=[grant(g) for g in access.paths],
        trees=[
            SandboxTreeStatus(
                path=redact_path(t.path, home),
                grant=t.grant,
                sources=[redact_path(s, home) for s in t.sources],
            )
            for t in access.trees
        ],
        excluded=[
            SandboxExcludedStatus(
                path=redact_path(t.path, home),
                source=redact_path(t.source, home),
                reason=t.reason,
            )
            for t in access.excluded
        ],
        denied=[
            SandboxDenyStatus(path=redact_path(d.path, home), builtin=d.builtin)
            for d in access.denied
        ],
        problem=problem,
    )


def _slot_status(
    slot: str,
    tool: str,
    network: NetworkPolicy,
    access: ResolvedAccess,
    access_problems: list[str],
    platform: str,
) -> SandboxSlotStatus:
    applied = next(
        agent.network.contract_applied for agent in CLI_AGENTS if agent.name == tool
    )
    problems: list[SandboxProblem] = []

    def add(setting: SandboxSetting, reason: str) -> None:
        if reason and all(p.reason != reason for p in problems):
            problems.append(SandboxProblem(setting=setting, reason=reason))

    if applied:
        add("network", unsupported_network_reason(tool, network, platform))
        for reason in access_problems:
            add("grants", reason)
        for grant in access.entries():
            add("grants", unsupported_grant_reason(tool, grant.access))
    return SandboxSlotStatus(
        slot=slot,
        tool=tool,
        contract_applied=applied,
        network=network,
        problems=problems,
    )


#: ``(person_id, slot, setting, reason)`` for one slot that cannot start here.
SandboxProblemEntry = tuple[str, str, str, str]


def sandbox_problems(person_ids: list[str]) -> list[SandboxProblemEntry]:
    """Every slot that cannot start on this device, with the setting to open."""
    return [
        (member.person_id, slot.slot, problem.setting, problem.reason)
        for member in sandbox_status(person_ids).members
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
                SharedGrants(documents=[grant]), LocalGrants(), "", home, create=False
            )
        except SandboxContractError as exc:
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
            resolve_access(SharedGrants(), local, "", home)
        except SandboxContractError as exc:
            return _invalid(scope, path, "", str(exc))
        return GrantEvaluation(scope="deny", path=path, valid=True, present=True)
    try:
        local = LocalGrants.model_validate(
            {"paths": [{"path": path, "access": access}]}
        )
    except ValidationError as exc:
        return _invalid("local", path, access, _reason(exc))
    try:
        resolve_access(SharedGrants(), local, "", home)
    except SandboxContractError as exc:
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
