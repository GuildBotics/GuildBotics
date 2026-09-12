"""This device's agent environment as the Desktop shows it, and why turns may not start.

One reading answers the status card, the alert band, the permission preview,
and the judgement offered while a grant is being added. The device's part --
runtime, snapshot, DNS, logins -- comes from the environment's own status
module, in the same words a refused turn is given; the grants are resolved
the way a turn resolves them. What keeps work from starting here is one of
three things, and each is reported where it is fixed: the device's
environment as a whole, one AI CLI tool on it, or one slot's grants.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from guildbotics.app_api.models import (
    AgentEnvironmentStatusResponse,
    CliAgentUsageCheck,
    EnvironmentAccessStatus,
    EnvironmentDenyStatus,
    EnvironmentDnsStatus,
    EnvironmentGrantStatus,
    EnvironmentMemberStatus,
    EnvironmentProblem,
    EnvironmentRuntimeStatus,
    EnvironmentSlotStatus,
    EnvironmentSnapshotStatus,
    EnvironmentToolStatus,
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
    local_path_missing,
    redact_path,
    resolve_access,
    sensitive_grant_reason,
)
from guildbotics.intelligences.agent_environment.status import (
    device_status,
    login_command,
)
from guildbotics.intelligences.agent_runtime.usage import CLI_AGENT_USAGE_READERS
from guildbotics.intelligences.brains.cli_agent import get_cli_agent_mapping
from guildbotics.intelligences.cli_agents import cli_agent_info
from guildbotics.utils.i18n_tool import t


def agent_environment_status(
    person_ids: list[str],
    *,
    platform: str = sys.platform,
    build_output: list[str] | None = None,
    building_here: bool = False,
    usage_checks: dict[str, CliAgentUsageCheck] | None = None,
) -> AgentEnvironmentStatusResponse:
    """Read the device and resolve every slot of the given members against it.

    Nothing is created here: a document directory that does not exist yet is
    reported absent, exactly what the turn would create when it starts.

    Args:
        person_ids: The active agent members whose slots to resolve.
        platform: This device's platform.
        build_output: The tail of the build this process last ran.
        building_here: This process has just started a build.
        usage_checks: Latest completed probes, shared with the alert band.
    """
    home = Path.home()
    device = device_status(building_here=building_here)
    snapshot = device.snapshot
    access = device.access
    problem = device.filesystem_problem
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
        runtime=EnvironmentRuntimeStatus(
            available=device.runtime.available,
            reason=device.runtime.reason,
            version=device.runtime.runtime_version,
            home=device.runtime.home,
        ),
        snapshot=EnvironmentSnapshotStatus(
            state=snapshot.state if snapshot else "missing",
            name=snapshot.name if snapshot else "",
            detail=snapshot.detail if snapshot else device.declaration_problem,
            output=list(build_output or []),
        ),
        dns=EnvironmentDnsStatus(
            declared=device.dns.declared,
            nameservers=list(device.dns.nameservers),
            problem=device.dns.problem,
        ),
        tools=[
            EnvironmentToolStatus(
                name=tool.name,
                label=tool.label,
                config_reference=cli_agent_info(tool.name).config_reference,
                provisioned=tool.provisioned,
                credentials_saved=tool.credentials_saved,
                authentication_failed=tool.authentication_failed,
                login_command=login_command(tool.name, platform=platform),
                problem=tool.problem or _usage_problem(tool.name, usage_checks or {}),
                usage_supported=tool.name in CLI_AGENT_USAGE_READERS,
                usage_check=(usage_checks or {}).get(tool.name),
            )
            for tool in device.tools
        ],
        problem=device.refusal,
        problem_setting=device.setting,
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
            builtin=g.builtin,
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


#: ``(person_id, slot, setting, reason)``: one thing that keeps work from
#: starting here. ``setting`` says whose it is: a part of the device (person
#: and slot empty), ``tool`` (one AI CLI tool, named in the slot field; person
#: empty), or ``grants`` (one member's slot).
EnvironmentProblemEntry = tuple[str, str, str, str]


def _usage_problem(name: str, checks: dict[str, CliAgentUsageCheck]) -> str:
    check = checks.get(name)
    if check is None or check.status != "failed":
        return ""
    return t("app_api.errors.cli_agent_usage_failed", label=cli_agent_info(name).label)


def agent_environment_problems(
    person_ids: list[str], *, usage_checks: dict[str, CliAgentUsageCheck] | None = None
) -> list[EnvironmentProblemEntry]:
    """Current device/tool problems, by the setting that addresses them.

    A tool is reported when an active member uses it or its usage probe failed.
    Unused tools with no failed probe do not produce missing-login alerts.
    """
    status = agent_environment_status(person_ids, usage_checks=usage_checks)
    entries: list[EnvironmentProblemEntry] = []
    if status.problem_setting:
        entries.append(("", "", status.problem_setting, status.problem))
    used = {slot.tool for member in status.members for slot in member.slots}
    entries += [
        ("", tool.name, "tool", tool.problem)
        for tool in status.tools
        if tool.problem
        and (
            tool.name in used
            or (tool.usage_check and tool.usage_check.status == "failed")
        )
    ]
    entries += [
        (member.person_id, slot.slot, problem.setting, problem.reason)
        for member in status.members
        for slot in member.slots
        for problem in slot.problems
    ]
    return entries


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
            present=resolved.documents[-1].present,
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
