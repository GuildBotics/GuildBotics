"""The commands a member can run from the Desktop, and what each one needs.

The catalog lists the effective command of every logical name, as the runtime
resolves it for the member, and evaluates the requirements a command declares
(its brain, its child commands, the ports its Python code uses) against what
this device and the project have.
"""

from __future__ import annotations

import ast
import os
import shlex
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, cast

from guildbotics.app_api.command_files import (
    CommandFileService,
    command_arguments,
    command_inputs,
    file_revision,
    shadow_source,
)
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    CommandFileExecutionStatus,
    CommandOption,
    CommandOptionsResponse,
    CommandRequirement,
    CommandRunRequest,
    RoutineCommandOptionsResponse,
)
from guildbotics.commands.brains import is_brain_disabled
from guildbotics.commands.discovery import (
    command_source,
    get_shared_commands_root,
    iter_effective_commands,
    logical_command_name,
    resolve_command_path,
    resolve_command_reference,
)
from guildbotics.commands.metadata import (
    command_entries,
    default_command_label,
    load_command_metadata,
)
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_command_source,
)
from guildbotics.entities import Service
from guildbotics.environment.status import DeviceStatus, device_status
from guildbotics.intelligences.brains.factory import cli_agent_of
from guildbotics.intelligences.llm_providers import provider_env_keys
from guildbotics.runtime import Context
from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_config_path,
    get_primary_config_path,
    get_template_path,
)
from guildbotics.utils.person_id import person_config_directory

#: What a command needs: a requirement kind, and for ``cli_agent`` the tool
#: its brain runs ("" when the command does not say which).
_Need = tuple[str, str]


@dataclass
class _RequirementFacts:
    """What this device has for the requirement kinds, read once per listing.

    Every command runs in the isolated agent environment, so ``environment``
    is met when the device can start one, and its refusal is the reason when
    it cannot. An AI CLI tool runs there too, so ``cli_agent`` is met unless
    the tool itself cannot start a turn (not logged in), which is the reason
    then; what the device refuses is the ``environment``'s to say.
    """

    #: The kinds of service the project names a provider for.
    services: frozenset[str]

    @classmethod
    def of(cls, context: Context) -> _RequirementFacts:
        project = context.team.project
        return cls(
            services=frozenset(
                service.value
                for service in Service
                if project.is_available_service(service)
            )
        )

    @cached_property
    def device(self) -> DeviceStatus:
        return device_status()

    def unmet(self, kind: str, tool: str) -> str | None:
        """None when the need is met, else why ("" when there is no reason
        more specific than the kind)."""
        if kind == "environment":
            return self.device.refusal or None
        if kind == "cli_agent":
            return (self.device.tool(tool).refusal or None) if tool else None
        return None if self._satisfied(kind) else ""

    def _satisfied(self, kind: str) -> bool:
        if kind in {service.value for service in Service}:
            return kind in self.services
        if kind == "llm":
            return any(
                os.getenv(env_var)
                for env_var in provider_env_keys(get_config_path("")).values()
            )
        return True


def command_options(context: Context, person: str | None) -> CommandOptionsResponse:
    """Return the commands ``person`` (the context's member if none) can run."""
    member = _member_context(context, person)
    facts = _RequirementFacts.of(member)
    language_code = member.team.project.get_language_code()
    options = [
        _command_option(
            command, path, facts, member, load_command_metadata(path, language_code)
        )
        for command, path in iter_effective_commands(
            _command_roots(member.person.person_id),
            language_code,
            person_id=member.person.person_id,
        )
    ]
    return CommandOptionsResponse(
        options=sorted(options, key=lambda option: option.command)
    )


def routine_command_options(
    context: Context, person: str | None
) -> RoutineCommandOptionsResponse:
    """Return the catalog of commands selectable as member routine commands.

    A command is a routine candidate when it self-declares ``routine: true``
    in its metadata (frontmatter for ``.md`` / ``.yml``;
    ``COMMAND_METADATA`` for ``.py``). Discovery scans member, workspace and
    package-template command roots through a single pass, so both built-in
    routine workflows (such as ``workflows/ticket_driven_workflow``) and
    workspace-defined ones surface without a maintained file list.

    The "runs with no caller-supplied input" rule is not used to hide
    candidates: a declared routine that still has required arguments is
    returned with ``routine_eligible=False`` so the UI can explain why it
    cannot run, instead of silently dropping it.
    """
    member = _member_context(context, person)
    facts = _RequirementFacts.of(member)
    language_code = member.team.project.get_language_code()

    options: list[CommandOption] = []
    for command, path in iter_effective_commands(
        _routine_command_roots(member.person.person_id),
        language_code,
        person_id=member.person.person_id,
    ):
        metadata = load_command_metadata(path, language_code)
        if metadata.get("routine") is not True:
            continue
        option = _command_option(command, path, facts, member, metadata)
        option.routine_eligible = option.inputs.message != "required" and (
            option.inputs.defined_args == "hidden"
            or not any(argument.required for argument in option.arguments)
        )
        options.append(option)
    options.sort(key=lambda option: option.command)
    return RoutineCommandOptionsResponse(
        options=options,
        default_command=_default_routine_command(options),
    )


def command_file_execution_status(
    context: Context, file_id: str, person: str | None, expected_revision: str
) -> CommandFileExecutionStatus:
    """Return whether running the selected shared file as ``person`` runs it."""
    try:
        # An omitted person runs as the team default, so the status must be
        # evaluated for that member rather than for no member at all.
        member = _member_context(
            context, person or context.team.get_default_person_id() or None
        )
    except AppApiError as exc:
        if exc.code == "person_not_found":
            return CommandFileExecutionStatus(
                matches_selected_file=False,
                blocking_code="person_not_found",
                blocking_context={"identifier": person or ""},
            )
        raise
    path = _selected_file(member, file_id)
    command = logical_command_name(
        get_shared_commands_root(), path, member.team.project.get_language_code()
    )
    code, blocking_context, requirements = _evaluate_run_target(
        member, path, expected_revision, command
    )
    return CommandFileExecutionStatus(
        matches_selected_file=code is None,
        requirements=requirements,
        blocking_code=cast(Any, code),
        blocking_context=blocking_context,
    )


def guard_run_target(request: CommandRunRequest, context: Context) -> None:
    """Reject a manual run unless the request runs exactly the selected file.

    Resolves ``request.command`` against the expected file id/revision, so a
    file-changed, shadowing, command-mismatch or missing-requirement
    condition all block the run. The frontend execution-status is only an
    ahead-of-time hint; the backend is the authority.

    Args:
        request: The manual run request being guarded.
        context: Context of the member the run executes as. It must be the
            member the run itself resolves, or the guard would check a
            different file than the one that ends up running.
    """
    assert request.expected_command_file_id is not None
    assert request.expected_command_file_revision is not None
    code, blocking_context, _ = _evaluate_run_target(
        context,
        _selected_file(context, request.expected_command_file_id),
        request.expected_command_file_revision,
        request.command,
    )
    if code is not None:
        raise AppApiError(
            code,
            "command_not_runtime_target",
            status_code=409,
            context=blocking_context,
        )


def _selected_file(context: Context, file_id: str) -> Path:
    language_code = context.team.project.get_language_code()
    return CommandFileService(language_code).resolve_existing(file_id)


def _evaluate_run_target(
    context: Context,
    path: Path,
    expected_revision: str,
    command: str,
) -> tuple[str | None, dict[str, str], list[CommandRequirement]]:
    """Evaluate whether running ``command`` executes the selected file.

    ``command`` is the logical command the caller intends to run (the run
    request's command, or the file's own logical name for status). The check
    confirms that resolving ``command`` for this member lands on exactly the
    selected file, so a mismatched command name cannot ride a valid file
    id/revision. Returns ``(blocking_code, blocking_context, requirements)``;
    a ``None`` code means the file is the target and every requirement is
    satisfied.
    """
    language_code = context.team.project.get_language_code()

    data = path.read_bytes()
    current = file_revision(data)
    if current != expected_revision:
        return "command_file_changed", {"current_revision": current}, []

    resolved = (
        resolve_command_path(command, language_code, context.person.person_id)
        if command
        else None
    )
    if resolved is None or resolved.resolve(strict=False) != path.resolve(strict=False):
        return "command_file_shadowed", {"shadow_source": shadow_source(resolved)}, []

    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return (
            "command_file_invalid_source",
            {
                "message": "Command source must be valid UTF-8.",
                "reason": "invalid_utf8",
            },
            [],
        )
    try:
        validate_command_source(path.suffix, source)
    except CommandValidationError as exc:
        return exc.code, {"message": str(exc), **exc.context}, []

    metadata = load_command_metadata(path, language_code)
    requirements = _command_requirements(
        path, metadata, _RequirementFacts.of(context), context
    )
    if any(not requirement.satisfied for requirement in requirements):
        return "command_requirement_missing", {}, requirements
    return None, {}, requirements


def _member_context(context: Context, person: str | None) -> Context:
    if not person:
        return context
    member = next(
        (
            team_member
            for team_member in context.team.members
            if person in {team_member.person_id, team_member.name}
        ),
        None,
    )
    if member is None:
        raise AppApiError(
            "person_not_found",
            "person_not_found.plain",
            params={"person": person},
            context={
                "identifier": person,
                "available": [
                    team_member.person_id for team_member in context.team.members
                ],
            },
        )
    return context.clone_for(member)


def _command_roots(person_id: str) -> list[Path]:
    """Physical roots whose logical command names seed the general catalog."""
    member_root = person_config_directory(person_id) / "commands"
    try:
        primary = get_primary_config_path(Path())
    except WorkspaceNotConfiguredError:
        return []
    return [
        primary / member_root,
        get_shared_commands_root(),
    ]


def _routine_command_roots(person_id: str) -> list[Path]:
    """Roots scanned for routine candidates.

    Unlike :func:`_command_roots`, this includes the package templates so that
    built-in routine workflows are discovered through the same single pass as
    workspace-defined ones. The shared runtime resolver still decides which
    file each logical command resolves to.
    """
    return [
        *_command_roots(person_id),
        get_template_path() / "commands",
    ]


def _default_routine_command(options: list[CommandOption]) -> str:
    """Pick the routine command to seed / pre-select for a new member.

    A single eligible candidate is the default on its own; with several, the
    ticket workflow wins.
    """
    eligible = [option.command for option in options if option.routine_eligible]
    if TICKET_WORKFLOW_COMMAND in eligible:
        return TICKET_WORKFLOW_COMMAND
    return eligible[0] if eligible else ""


def _command_option(
    command: str,
    path: Path,
    facts: _RequirementFacts,
    context: Context,
    metadata: dict[str, Any],
) -> CommandOption:
    return CommandOption(
        command=command,
        label=str(metadata.get("name") or default_command_label(command)),
        description=str(metadata.get("description", "")),
        category=cast(Any, _command_category(command)),
        source=cast(Any, command_source(path)),
        path=path,
        arguments=command_arguments(path, metadata, command),
        inputs=command_inputs(metadata),
        requirements=_command_requirements(path, metadata, facts, context),
    )


def _command_category(command: str) -> str:
    if command.startswith("workflows/"):
        return "workflow"
    if command.startswith("functions/"):
        return "function"
    if command.startswith("examples/"):
        return "example"
    return "custom"


def _command_requirements(
    path: Path,
    metadata: dict[str, Any],
    facts: _RequirementFacts,
    context: Context,
) -> list[CommandRequirement]:
    # Every command runs in the isolated environment, whatever else it needs.
    needs = {
        ("environment", ""),
        *_command_requirement_kinds(path, metadata, context, set()),
    }
    requirements: list[CommandRequirement] = []
    for kind in sorted({kind for kind, _ in needs}):
        unmet = [
            facts.unmet(kind, tool) for need, tool in sorted(needs) if need == kind
        ]
        requirements.append(
            CommandRequirement(
                kind=cast(Any, kind),
                satisfied=all(reason is None for reason in unmet),
                message=next((reason for reason in unmet if reason), ""),
            )
        )
    return requirements


def _command_requirement_kinds(
    path: Path,
    metadata: dict[str, Any],
    context: Context,
    seen: set[Path],
) -> set[_Need]:
    resolved_path = path.resolve(strict=False)
    if resolved_path in seen:
        return set()
    seen.add(resolved_path)

    kinds: set[_Need] = _direct_command_requirement_kinds(path, metadata, context)
    for entry in command_entries(metadata):
        kinds.update(
            _command_entry_requirement_kinds(path.parent, entry, context, seen)
        )
    return kinds


def _direct_command_requirement_kinds(
    path: Path, metadata: dict[str, Any], context: Context
) -> set[_Need]:
    if path.suffix == ".md":
        kind = _brain_requirement_kind(metadata.get("brain", "default"), context)
        return {kind} if kind else set()
    if path.suffix == ".py":
        return _python_requirement_kinds(path)
    return set()


def _brain_requirement_kind(brain_value: object, context: Context) -> _Need | None:
    brain = str(brain_value).strip()
    if is_brain_disabled(brain):
        return None

    try:
        tool = cli_agent_of(context.person.person_id, brain)
    except Exception:
        # A mapping that does not load names no need: running the command
        # says why it cannot run.
        return None
    return ("llm", "") if tool is None else ("cli_agent", tool)


def _command_entry_requirement_kinds(
    base_dir: Path,
    entry: object,
    context: Context,
    seen: set[Path],
) -> set[_Need]:
    if isinstance(entry, str):
        return _referenced_command_requirement_kinds(base_dir, entry, context, seen)
    if not isinstance(entry, dict):
        return set()

    if any(key in entry for key in {"prompt", "print"}):
        return _inline_markdown_requirement_kinds(entry, context)
    if "python" in entry:
        return _inline_python_requirement_kinds(entry)
    if any(key in entry for key in {"script", "to_html", "to_pdf"}):
        return set()
    command_text = entry.get("command")
    if command_text is not None:
        return _referenced_command_requirement_kinds(
            base_dir, str(command_text), context, seen
        )
    path_text = entry.get("path") or entry.get("name")
    if path_text is not None:
        return _referenced_command_requirement_kinds(
            base_dir, str(path_text), context, seen
        )
    return set()


def _inline_markdown_requirement_kinds(entry: dict, context: Context) -> set[_Need]:
    if "print" in entry:
        return set()
    kind = _brain_requirement_kind(entry.get("brain", "default"), context)
    if kind is None:
        return set()
    return {kind}


def _inline_python_requirement_kinds(entry: dict) -> set[_Need]:
    code = entry.get("python")
    if not isinstance(code, str):
        return set()
    try:
        module = ast.parse(code)
    except Exception:
        return set()
    return _python_module_requirement_kinds(module)


def _referenced_command_requirement_kinds(
    base_dir: Path,
    command_text: str,
    context: Context,
    seen: set[Path],
) -> set[_Need]:
    command_name = _command_reference_name(command_text)
    if not command_name:
        return set()
    try:
        resolved = resolve_command_reference(base_dir, command_name, context)
    except Exception:
        return set()
    metadata = load_command_metadata(resolved, context.team.project.get_language_code())
    return _command_requirement_kinds(resolved, metadata, context, seen)


def _command_reference_name(command_text: str) -> str:
    try:
        parts = shlex.split(command_text)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _python_requirement_kinds(path: Path) -> set[_Need]:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return _python_module_requirement_kinds(module)


def _python_module_requirement_kinds(module: ast.Module) -> set[_Need]:
    names: set[str] = set()
    attrs: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            for alias in node.names:
                modules.add(alias.name)
                names.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)

    # A command needs a kind of service when it uses its port.
    kinds = {
        service.value
        for service in Service
        if f"guildbotics.runtime.{service.value}" in modules
        or service.value.title().replace("_", "") in names
        or {f"get_{service.value}", f"create_{service.value}"} & attrs
    }
    if modules & {"guildbotics.intelligences.functions"} or attrs & {"get_brain"}:
        kinds.add("llm")
    if names & {"CliAgentBrain"}:
        kinds.add("cli_agent")
    # A Python command names no tool, so no tool of its own refuses it here.
    return {(kind, "") for kind in kinds}
