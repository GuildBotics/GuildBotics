from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shlex
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from guildbotics.app_api.activity_history import (
    ActivityLifecycle,
    build_activity_history,
    lifecycle_from_run,
    lifecycle_from_session,
    parse_timestamp,
    run_subject_id,
)
from guildbotics.app_api.agent_environment_status import (
    EnvironmentProblemEntry,
    agent_environment_problems,
    agent_environment_status,
)
from guildbotics.app_api.agent_streams import collapse_assistant_streams
from guildbotics.app_api.cli_agent_usage import CliAgentUsageCache
from guildbotics.app_api.command_files import CommandFileService, file_revision
from guildbotics.app_api.command_input_files import command_cwd
from guildbotics.app_api.config_revisions import apply_config_write
from guildbotics.app_api.diagnostics import ScenarioDiagnosticsService
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.intelligences import CLI_BRAIN_CLASS
from guildbotics.app_api.lifecycle import RuntimeLifecycleService
from guildbotics.app_api.models import (
    ActivityHistoryResponse,
    AgentEnvironmentStatusResponse,
    AgentFieldOption,
    AgentFieldStateResponse,
    ChatReceiveResetResponse,
    CliAgentUsageCheck,
    CliAgentUsageResponse,
    CommandAuthoringApplyRequest,
    CommandAuthoringApplyResponse,
    CommandAuthoringChange,
    CommandAuthoringRequest,
    CommandAuthoringResponse,
    CommandFileCreateRequest,
    CommandFileDetail,
    CommandFileExecutionStatus,
    CommandFilesResponse,
    CommandFileUpdateRequest,
    CommandOption,
    CommandOptionsResponse,
    CommandRequirement,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    MemberSummary,
    MemoryEvent,
    MemoryEventsResponse,
    ProjectStatusOptionsRequest,
    ProjectStatusOptionsResponse,
    ProjectSummary,
    RoutineCommandOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    SystemAlertsResponse,
    TeamSummary,
    TraceDetailResponse,
    TraceRecord,
    TracesResponse,
    TraceSummary,
    TranscriptSettingsStatus,
    TranscriptSettingsUpdateRequest,
    TroubleshootingFocus,
    TroubleshootingRequest,
    TroubleshootingResponse,
    VerifyResponse,
    to_command_arguments,
    to_command_inputs,
)
from guildbotics.app_api.system_alerts import SystemAlertService
from guildbotics.app_api.verify import VerifyService
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.capabilities.command_failures import (
    command_failure_payload,
)
from guildbotics.capabilities.github_activity_events import (
    refresh_github_activity_events,
)
from guildbotics.capabilities.member_memory_audit import (
    MemoryAuditStore,
    parse_memory_audit_timestamp,
)
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.authoring import CommandAuthoringResult
from guildbotics.commands.brains import is_brain_disabled
from guildbotics.commands.discovery import (
    command_source,
    get_shared_commands_root,
    is_within,
    iter_effective_commands,
    logical_command_name,
    resolve_command_path,
    resolve_command_reference,
)
from guildbotics.commands.formats import EXTENSION_BY_FORMAT
from guildbotics.commands.metadata import (
    CommandAccess,
    default_command_label,
    load_command_metadata,
    parse_command_arguments,
    parse_command_input_policy,
)
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_command_source,
)
from guildbotics.drivers import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.drivers.command_runner import (
    CommandRunner,
    prepare_command,
    run_main_command,
)
from guildbotics.drivers.execution import (
    ExecutionStatusPublisher,
    TaskRunCoordinator,
    WorkRejectedError,
)
from guildbotics.editions import get_edition
from guildbotics.editions.simple.setup_service import (
    PersonConfigSummary,
    SimplePersonSetupService,
)
from guildbotics.entities import Person, Project, Service, Team
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.intelligences.agent_environment.contract import exchange_dir
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    doctor,
)
from guildbotics.intelligences.agent_environment.snapshot import build_snapshot
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
)
from guildbotics.intelligences.agent_runtime.environment import inspected_directories
from guildbotics.intelligences.brains.cli_agent import CliAgentExecutionError
from guildbotics.intelligences.cli_agents import CLI_AGENTS, resolve_cli_agent_path
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from guildbotics.observability import new_id, trace_scope
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import (
    DEFAULT_DIAGNOSTICS_MAX_BYTES,
    DiagnosticsStore,
)
from guildbotics.observability.event_types import GITHUB_WORK_TARGET_EVENT_TYPE
from guildbotics.observability.interactive_sessions import InteractiveSessionStore
from guildbotics.observability.session_transcripts import (
    transcript_detail,
    transcript_retention_days,
)
from guildbotics.observability.trace_title import CompletionSummary
from guildbotics.runtime import Context
from guildbotics.runtime.live_state import LiveStatePort
from guildbotics.runtime.member_context import resolve_person
from guildbotics.runtime.service_lock import ServiceLockUnavailableError
from guildbotics.runtime.trace_presentations import normalize_trace_presentation
from guildbotics.utils.env_loader import (
    HOME_ENV_PROTECTED_KEYS,
    apply_debug_env_to_process,
    read_debug_env,
    read_workspace_secrets,
    write_debug_env,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    WorkspaceNotConfiguredError,
    apply_workspace_root,
    get_machine_state_root,
    get_person_config_path,
    get_primary_config_dir,
    get_primary_config_path,
    get_template_path,
    get_workspace_local_path,
    get_workspace_root,
    get_workspace_state_path,
    get_workspace_work_path,
    load_yaml_file,
)
from guildbotics.utils.workspace_state import write_active_workspace

WORKSPACE_DOTENV_PROTECTED_KEYS = {
    GUILDBOTICS_WORKSPACE_ROOT,
    *HOME_ENV_PROTECTED_KEYS,
}
MIN_MEMORY_DOCUMENT_PATH_PARTS = 2
ACTIVITY_SYNC_COOLDOWN_SECONDS = 5 * 60
ACTIVITY_SYNC_STATE_FILE = "activity_sync_weeks.json"
ACTIVITY_SYNC_PERIOD_PARTS = 2


class _UseProcessDataDir:
    pass


_USE_PROCESS_DATA_DIR = _UseProcessDataDir()

#: Lines of build output the status card can show.
ENVIRONMENT_BUILD_OUTPUT_LINES = 200


def _activity_sync_state_path() -> Path:
    return get_workspace_local_path("run", ACTIVITY_SYNC_STATE_FILE)


def _completed_activity_weeks() -> set[tuple[str, str]]:
    try:
        payload = json.loads(_activity_sync_state_path().read_text(encoding="utf-8"))
        return {
            tuple(item)
            for item in payload.get("completed", [])
            if len(item) == ACTIVITY_SYNC_PERIOD_PARTS
        }
    except (OSError, ValueError, TypeError):
        return set()


def _mark_activity_week_completed(period: tuple[str, str]) -> None:
    completed = _completed_activity_weeks()
    completed.add(period)
    path = _activity_sync_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"completed": sorted(completed)}), encoding="utf-8")
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class _Execution:
    """One command the Desktop runs, and how its run is reported."""

    command: str
    #: What the run is called in the runtime status, its trace and its events.
    label: str
    #: Where it runs; derived only once the run is accepted for the workspace.
    cwd: Callable[[], Path]
    #: The App API error code a failed run becomes.
    failure_code: str
    failure_status: int = 400
    args: Sequence[str] = ()
    attributes: Mapping[str, str] | None = None
    #: The result the command must return, when the caller reads it.
    result_type: type[BaseModel] | None = None

    def failure(self, exc: CommandError | CliAgentExecutionError) -> AppApiError:
        """The App API error a failed run becomes.

        A command drives foreign agents: an AI CLI tool that exits non-zero, a
        provider that rejects the credential, a response the prompt cannot
        use. Each is a failed run the screen must explain with its own reason.
        """
        return AppApiError(
            self.failure_code, reason=str(exc), status_code=self.failure_status
        )


class AppRuntime:
    def __init__(
        self,
        event_bus: EventBus,
        *,
        stop_timeout_seconds: float = 10.0,
        diagnostics_store: DiagnosticsStore | None = None,
        load_workspace_environment: bool = False,
    ) -> None:
        self._event_bus = event_bus
        self._diagnostics_store = diagnostics_store
        self._system_service_run_id = new_id()
        self._system_alerts = SystemAlertService(diagnostics_store)
        self._lock = threading.Lock()
        self._activity_sync_lock = threading.Lock()
        self._activity_sync_attempts: dict[tuple[str, str], float] = {}
        self._running_command_id: str | None = None
        #: Every accepted manual run, read-only ones included: the workspace
        #: does not switch under any of them.
        self._accepted_command_ids: set[str] = set()
        self.on_workspace_changed: Callable[[Path], None] | None = None
        self._execution_status = ExecutionStatusPublisher()
        self._execution = TaskRunCoordinator(self._execution_status)
        self._cli_agent_usage = CliAgentUsageCache(
            lambda: (
                self._diagnostics_store.latest_system_trace_id() or ""
                if self._diagnostics_store is not None
                else ""
            )
        )
        self._environment_build_lock = threading.Lock()
        self._environment_build: threading.Thread | None = None
        #: The tail of the build this process last ran, for the status card.
        self._environment_build_output: deque[str] = deque(
            maxlen=ENVIRONMENT_BUILD_OUTPUT_LINES
        )
        self._loaded_dotenv_keys: set[str] = set()
        self._workspace_sync = WorkspaceSyncService(
            on_live_publisher=self.set_live_state,
            on_owner_transfer=self._execution.mark_interrupted,
        )
        if load_workspace_environment:
            self._load_workspace_env()
        self._lifecycle = RuntimeLifecycleService(
            event_bus=event_bus,
            context_factory=self._get_context,
            stop_timeout_seconds=stop_timeout_seconds,
            execution_coordinator=self._execution,
            before_start=self._prepare_runtime_start,
        )

    @property
    def system_service_run_id(self) -> str:
        return self._system_service_run_id

    def set_live_state(self, live_state: LiveStatePort | None) -> None:
        """Attach the process-wide Hub relay to the shared execution boundary."""
        self._execution_status.set_live_state(live_state)

    @property
    def workspace_sync_service(self) -> WorkspaceSyncService:
        """Expose the process-wide sync/relay service to the API composition root."""
        return self._workspace_sync

    def get_config_status(self) -> ConfigStatus:
        try:
            workspace: Path | None = get_workspace_root()
        except WorkspaceNotConfiguredError:
            # First launch: no workspace is selected yet. Never fall back to
            # the process cwd — it may be a source checkout, and reporting it
            # would let Setup create `.guildbotics/` there without an explicit
            # choice.
            workspace = None
        config_dir = get_primary_config_dir() or (
            workspace / ".guildbotics" / "config" if workspace is not None else None
        )
        project_file = (
            config_dir / "team" / "project.yml" if config_dir is not None else None
        )
        return ConfigStatus(
            cwd=exchange_dir(),
            workspace=workspace,
            config_dir=config_dir,
            project_file=project_file,
            project_file_exists=project_file is not None and project_file.exists(),
            storage_dir=workspace,
            machine_state_dir=get_machine_state_root(),
            workspace_state_dir=(
                workspace / ".guildbotics" / "state" if workspace is not None else None
            ),
            workspace_local_dir=(
                workspace / ".guildbotics" / "local" if workspace is not None else None
            ),
        )

    def set_workspace(self, workspace_dir: Path) -> ConfigStatus:
        with self._lock:
            if self._accepted_command_ids:
                raise _workspace_switch_blocked_error(self.get_scheduler_status())
            return self._set_workspace(workspace_dir)

    def _set_workspace(self, workspace_dir: Path) -> ConfigStatus:
        workspace = workspace_dir.expanduser().resolve()
        if not workspace.exists():
            raise AppApiError(
                "workspace_not_found",
                context={"workspace_dir": str(workspace)},
                status_code=400,
            )
        if not workspace.is_dir():
            raise AppApiError(
                "workspace_not_directory",
                context={"workspace_dir": str(workspace)},
                status_code=400,
            )
        # Reject up front so a running service is not force-stopped just
        # because a switch was requested.
        status = self.get_scheduler_status()
        if status.has_active_work:
            raise _workspace_switch_blocked_error(status)
        # Force-stop anything that slipped in between the check above and here.
        # A forced stop can still time out (uncancellable work, drain timeout),
        # so re-check and abort rather than switching cwd/env under live work.
        stopped = self.stop_scheduler(force=True)
        if stopped.has_active_work:
            raise _workspace_switch_blocked_error(stopped)
        if self._diagnostics_store is not None:
            self._diagnostics_store.finish_system_session()
        # Stopped before the switch so the queue of the workspace being left
        # cannot commit into the one being entered. A queue still finishing a
        # fetch or push holds that workspace's repository, so the switch waits
        # rather than leaving two of them running side by side.
        if not self._workspace_sync.deactivate():
            raise AppApiError(
                "workspace_switch_blocked",
                status_code=409,
            )
        os.chdir(workspace)
        write_active_workspace(workspace)
        apply_workspace_root(workspace)
        if self.on_workspace_changed is not None:
            self.on_workspace_changed(workspace)
        self._load_workspace_env()
        if self._diagnostics_store is not None:
            self._diagnostics_store.start_system_session(self._system_service_run_id)
            self._diagnostics_store.start_maintenance()
        self._workspace_sync.activate()
        return self.get_config_status()

    def get_team_summary(self) -> TeamSummary:
        members: Sequence[Person | PersonConfigSummary]
        try:
            context = self._get_context()
        except AppApiError:
            status = self.get_config_status()
            if status.project_file_exists:
                raise
            project = Project()
            members = (
                SimplePersonSetupService().list_person_configs(
                    config_dir=status.config_dir
                )
                if status.config_dir is not None
                else []
            )
            default_person_id = ""
        else:
            project = context.team.project
            members = context.team.members
            default_person_id = context.team.get_default_person_id()
        return TeamSummary(
            project=ProjectSummary(
                name=getattr(project, "name", ""),
                language_code=project.get_language_code(),
                language_name=project.get_language_name(),
            ),
            members=[
                MemberSummary(
                    person_id=member.person_id,
                    name=member.name,
                    person_type=member.person_type,
                    is_active=member.is_active,
                    roles=sorted(
                        member.roles.keys()
                        if isinstance(member.roles, dict)
                        else member.roles
                    ),
                )
                for member in members
            ],
            default_person_id=default_person_id,
        )

    def get_command_options(self, person: str | None = None) -> CommandOptionsResponse:
        context = self._command_options_context(person)
        options = self._collect_command_options(context)
        return CommandOptionsResponse(
            options=sorted(options.values(), key=lambda option: option.command)
        )

    def _command_file_service(self) -> CommandFileService:
        language_code = self._get_context().team.project.get_language_code()
        return CommandFileService(language_code)

    def _write_shared_commands[T](self, apply: Callable[[], T]) -> T:
        """Run a change to the shared command files through the write boundary.

        Commands live under ``config/commands``, so they are shared state like
        the rest of config: the service compares a revision and then writes,
        and synchronization must not check the hub's content out between the
        two. Applying a reviewed change set writes several files, which is one
        change for the same reason.
        """
        config_dir = get_primary_config_dir()
        if config_dir is None:
            raise WorkspaceNotConfiguredError(
                "No workspace is selected, so there are no shared commands."
            )
        return apply_config_write(config_dir, apply).result

    def list_command_files(self) -> CommandFilesResponse:
        return self._command_file_service().list_files()

    def get_command_file(self, file_id: str) -> CommandFileDetail:
        return self._command_file_service().read_file(file_id)

    def create_command_file(
        self, request: CommandFileCreateRequest
    ) -> CommandFileDetail:
        return self._write_shared_commands(
            lambda: self._command_file_service().create_file(
                request.command, request.format, request.content
            )
        )

    def update_command_file(
        self, file_id: str, request: CommandFileUpdateRequest
    ) -> CommandFileDetail:
        return self._write_shared_commands(
            lambda: self._command_file_service().update_file(
                file_id, request.content, request.expected_revision
            )
        )

    def delete_command_file(
        self, file_id: str, expected_revision: str
    ) -> CommandFilesResponse:
        return self._write_shared_commands(
            lambda: self._command_file_service().delete_file(file_id, expected_revision)
        )

    async def author_command(
        self, request: CommandAuthoringRequest
    ) -> CommandAuthoringResponse:
        """Return one answer or reviewed-change proposal for shared commands."""

        def message(_access: CommandAccess) -> str:
            return json.dumps(
                {
                    "mode": request.mode,
                    "command": request.command,
                    "format": request.format,
                    "current_content": request.content,
                    "instruction": request.message,
                    "available_commands": self._command_authoring_context(),
                },
                ensure_ascii=False,
            )

        trace_id, outcome = await self._execute_command(
            _Execution(
                command="assistants/author_command",
                label=f"author:{request.command or 'new-command'}",
                args=[f"conversation_id={request.conversation_id}"],
                cwd=partial(_assistant_cwd, "command-authoring"),
                failure_code="command_authoring_failed",
                failure_status=502,
                attributes={
                    "command_authoring.conversation_id": request.conversation_id
                },
                result_type=CommandAuthoringResult,
            ),
            person=request.person,
            message=message,
        )
        result = cast(CommandAuthoringResult, outcome.result)
        return CommandAuthoringResponse(
            trace_id=trace_id,
            message=result.message,
            action=result.action,
            changes=[
                CommandAuthoringChange(
                    operation=change.operation,
                    command=change.command,
                    format=change.format,
                    relative_path=(
                        f"{change.command}{EXTENSION_BY_FORMAT[change.format]}"
                    ),
                    content=change.content,
                    file_id=request.file_id if change.operation == "update" else "",
                    expected_revision=(
                        request.revision if change.operation == "update" else ""
                    ),
                )
                for change in result.changes
            ],
        )

    def apply_command_authoring(
        self, request: CommandAuthoringApplyRequest
    ) -> CommandAuthoringApplyResponse:
        """Apply a user-reviewed AI command change set."""
        return CommandAuthoringApplyResponse(
            files=self._write_shared_commands(
                lambda: self._command_file_service().apply_authoring_changes(
                    request.changes
                )
            )
        )

    def _command_authoring_context(self) -> list[dict[str, str]]:
        """Return effective shared command sources for read-only AI inspection."""
        service = self._command_file_service()
        return [
            {
                "command": detail.command,
                "format": detail.format,
                "relative_path": detail.relative_path,
                "content": detail.content,
            }
            for summary in service.list_files().files
            for detail in [service.read_file(summary.id)]
        ]

    def get_command_file_execution_status(
        self, file_id: str, person: str | None, expected_revision: str
    ) -> CommandFileExecutionStatus:
        try:
            # An omitted person runs as the team default, so the status must be
            # evaluated for that member rather than for no member at all.
            context = self._command_options_context(
                person or self._get_context().team.get_default_person_id() or None
            )
        except AppApiError as exc:
            if exc.code == "person_not_found":
                return CommandFileExecutionStatus(
                    matches_selected_file=False,
                    blocking_code="person_not_found",
                    blocking_context={"identifier": person or ""},
                )
            raise
        path = self._command_file_service().resolve_existing(file_id)
        language_code = context.team.project.get_language_code()
        command = logical_command_name(get_shared_commands_root(), path, language_code)
        code, blocking_context, requirements = self._evaluate_run_target(
            context, file_id, expected_revision, command
        )
        return CommandFileExecutionStatus(
            matches_selected_file=code is None,
            requirements=requirements,
            blocking_code=cast(Any, code),
            blocking_context=blocking_context,
        )

    def _evaluate_run_target(
        self,
        context: Context,
        file_id: str,
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
        path = self._command_file_service().resolve_existing(file_id)
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
        if resolved is None or resolved.resolve(strict=False) != path.resolve(
            strict=False
        ):
            context_data = {"shadow_source": _shadow_source(resolved)}
            if resolved is not None:
                context_data["resolved_relative_path"] = _resolved_display(resolved)
            return "command_file_shadowed", context_data, []

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
            path, metadata, self.is_github_integration_enabled(), context
        )
        if any(not requirement.satisfied for requirement in requirements):
            return "command_requirement_missing", {}, requirements
        return None, {}, requirements

    def get_routine_command_options(
        self, person: str | None = None
    ) -> RoutineCommandOptionsResponse:
        """Return the catalog of commands selectable as member routine commands.

        A command is a routine candidate when it self-declares ``routine: true``
        in its metadata (frontmatter for ``.md`` / ``.yml``;
        ``COMMAND_METADATA`` for ``.py``). Discovery scans member, workspace and
        package-template command roots through a single pass, so both built-in
        routine workflows (such as ``workflows/ticket_driven_workflow``) and
        workspace-defined ones surface without an edition-maintained file list.

        The "runs with no caller-supplied input" rule is not used to hide
        candidates: a declared routine that still has required arguments is
        returned with ``routine_eligible=False`` so the UI can explain why it
        cannot run, instead of silently dropping it.
        """
        context = self._command_options_context(person)
        github_enabled = self.is_github_integration_enabled()
        language_code = context.team.project.get_language_code()

        options: dict[str, CommandOption] = {}
        for command, path in iter_effective_commands(
            _routine_command_roots(context.person.person_id),
            language_code,
            person_id=context.person.person_id,
        ):
            metadata = load_command_metadata(path, language_code)
            if not _is_routine_command(metadata):
                continue
            option = _command_option(
                command=command,
                path=path,
                github_enabled=github_enabled,
                context=context,
                metadata=metadata,
            )
            options[command] = option.model_copy(
                update={
                    "routine_eligible": option.inputs.message != "required"
                    and (
                        option.inputs.defined_args == "hidden"
                        or not any(argument.required for argument in option.arguments)
                    )
                }
            )
        ordered = sorted(options.values(), key=lambda option: option.command)
        return RoutineCommandOptionsResponse(
            options=ordered,
            default_command=_default_routine_command(ordered),
        )

    def _command_options_context(self, person: str | None) -> Context:
        context = self._get_context()
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

    def _collect_command_options(self, context: Context) -> dict[str, CommandOption]:
        github_enabled = self.is_github_integration_enabled()
        options: dict[str, CommandOption] = {}
        for command, path in iter_effective_commands(
            _command_roots(context.person.person_id),
            context.team.project.get_language_code(),
            person_id=context.person.person_id,
        ):
            if command in options:
                continue
            options[command] = _command_option(
                command=command,
                path=path,
                github_enabled=github_enabled,
                context=context,
            )
        return options

    def _guard_run_target(self, request: CommandRunRequest, context: Context) -> None:
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
        code, blocking_context, _ = self._evaluate_run_target(
            context,
            request.expected_command_file_id,
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

    async def run_command(self, request: CommandRunRequest) -> CommandRunResponse:
        trace_id, outcome = await self._execute_command(
            _Execution(
                command=request.command,
                label=request.command,
                args=request.args,
                cwd=lambda: command_cwd(request.cwd) or _default_command_cwd(),
                failure_code="command_error",
            ),
            person=request.person,
            message=lambda _access: request.message,
            expected_workspace=request.expected_workspace,
            guard=(
                partial(self._guard_run_target, request)
                if request.expected_command_file_id is not None
                else None
            ),
        )
        return CommandRunResponse(trace_id=trace_id, output=outcome.text_output)

    async def _execute_command(
        self,
        execution: _Execution,
        *,
        person: str | None,
        message: Callable[[CommandAccess], str],
        expected_workspace: Path | None = None,
        guard: Callable[[Context], None] | None = None,
    ) -> tuple[str, CommandOutcome]:
        """Run one command the Desktop starts, as manual work under a new trace.

        A command that declares itself read-only takes no manual-command
        reservation and no exclusive work slot: its turns can change nothing,
        so it stays usable while another command or that member's scheduled
        work runs.

        Args:
            execution: The command and how its run is reported.
            person: Requested member identifier, or ``None`` for the team default.
            message: Builds the command's input from what it declares.
            expected_workspace: Workspace the caller expects to be selected.
            guard: Checks the command against the member it runs as before
                anything starts.

        Returns:
            The run's trace id and the command's outcome.

        Raises:
            AppApiError: If the member cannot be resolved, the runtime rejects
                the work, or the command fails.
        """
        trace_id = new_id()
        # The run is accepted for the workspace before anything is derived from
        # it, and the workspace cannot switch until the run is released.
        self._reserve_command(trace_id, expected_workspace, exclusive=False)
        try:
            context = self._get_context()
            # Resolve the member up front: the guard must check the file that
            # this very member runs, and an omitted person would otherwise
            # resolve twice (placeholder context for the guard, team default
            # for the run).
            acting = self._resolve_execution_person(context, person, execution.label)
            if guard is not None:
                guard(context.clone_for(acting))
            # The one reading of the command: its slot, its tracking, its input
            # and the run itself all go by this runner. A command that cannot be
            # resolved never ran, so it leaves no trace.
            try:
                runner = prepare_command(
                    context,
                    execution.command,
                    execution.args,
                    acting.person_id,
                    execution.cwd(),
                )
            except CommandError as exc:
                raise execution.failure(exc) from exc
            runner.context.pipe = message(runner.access)
            read_only = runner.access.read_only
            if not read_only:
                self._reserve_command(trace_id)
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()

            def _cancel() -> None:
                if task is not None:
                    loop.call_soon_threadsafe(task.cancel)

            with (
                self._execution.track_work(
                    source="manual",
                    person_id=acting.person_id,
                    command=execution.label,
                    work_id=trace_id,
                    cancel=_cancel,
                    exclusive=not read_only,
                ),
                trace_scope(
                    "manual",
                    command=execution.label,
                    person_id=acting.person_id,
                    trace_id=trace_id,
                    attributes=execution.attributes,
                ),
            ):
                outcome = await self._run_command_traced(execution, runner)
        except WorkRejectedError as exc:
            raise AppApiError(
                "work_rejected", reason=str(exc), status_code=409
            ) from exc
        finally:
            self._release_command(trace_id)
        return trace_id, outcome

    def _resolve_execution_person(
        self, context: Context, person_identifier: str | None, command: str
    ) -> Person:
        """Resolve the member a manual run executes as.

        Args:
            context: Base context holding the team.
            person_identifier: Requested member identifier, or ``None`` for the
                team default.
            command: Command label used in a selection-failure event.

        Returns:
            Person: The requested member, or the team default when the request
            names none.

        Raises:
            AppApiError: If no member could be resolved.
        """
        try:
            return resolve_person(context.team, person_identifier, allow_default=True)
        except PersonSelectionRequiredError as exc:
            available = list(exc.available)
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": command,
                    "code": "person_selection_required",
                    "available": available,
                },
            )
            raise AppApiError(
                "person_selection_required",
                params={"available": ", ".join(available) if available else "none"},
                context={"available": available},
            ) from exc
        except PersonNotFoundError as exc:
            available = list(exc.available)
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": command,
                    "code": "person_not_found",
                    "identifier": exc.identifier,
                    "available": available,
                },
            )
            raise AppApiError(
                "person_not_found",
                "person_not_found.with_available",
                params={
                    "person": exc.identifier,
                    "available": ", ".join(available) if available else "none",
                },
                context={"identifier": exc.identifier, "available": available},
            ) from exc

    async def _run_command_traced(
        self, execution: _Execution, runner: CommandRunner
    ) -> CommandOutcome:
        # This opens the run's trace, so it is the only layer that can say the
        # whole run started and ended. Events carry the resolved person so an
        # omitted request person still shows the member the run belongs to.
        person_id = runner.context.person.person_id
        self._event_bus.publish_event(
            "command.started", {"command": execution.label, "person": person_id}
        )
        try:
            # Closing the run's context is part of the run: a close that fails
            # ends the run as failed rather than leaving it unended.
            try:
                outcome = await run_main_command(runner, source="manual")
            finally:
                await runner.context.aclose()
            if execution.result_type is not None and not isinstance(
                outcome.result, execution.result_type
            ):
                raise CommandError(
                    f"Command '{execution.command}' did not return a "
                    f"{execution.result_type.__name__}."
                )
        except BaseException as exc:
            # Cancellation lands here as well: a force stop cancels this task,
            # and the run it started has to be reported as ended either way.
            # Anything but a failed command is a defect: it stays a generic 500,
            # so internal wording never reaches the client.
            failed = isinstance(exc, CommandError | CliAgentExecutionError)
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": execution.label,
                    "person": person_id,
                    **command_failure_payload(exc),
                    **({"message": str(exc)} if failed else {}),
                },
            )
            if isinstance(exc, CommandError | CliAgentExecutionError):
                raise execution.failure(exc) from exc
            raise
        self._event_bus.publish_event(
            "command.finished", {"command": execution.label, "person": person_id}
        )
        return outcome

    def start_scheduler(self, request: SchedulerStartRequest) -> RuntimeStatus:
        try:
            return self._lifecycle.start(request)
        except ServiceLockUnavailableError as exc:
            metadata = exc.metadata
            raise AppApiError(
                "service_already_running",
                status_code=409,
                context=(
                    {
                        "owner": metadata.owner,
                        "pid": metadata.pid,
                        "workspace": metadata.workspace,
                        "started_at": metadata.started_at,
                    }
                    if metadata is not None
                    else {}
                ),
            ) from exc
        except Exception:
            self._execution.set_owner_check(None)
            raise

    def _prepare_runtime_start(self) -> None:
        """Prepare synchronization and ownership after service.lock is held."""
        self._execution.set_owner_check(self._workspace_sync.prepare_service_owner())

    def stop_scheduler(self, *, force: bool = False) -> RuntimeStatus:
        status = self._lifecycle.stop(force=force)
        self._execution.set_owner_check(None)
        return status

    def get_scheduler_status(self) -> RuntimeStatus:
        return self._lifecycle.get_status()

    def get_system_alerts(self) -> SystemAlertsResponse:
        return self._system_alerts.list_alerts(
            self.get_scheduler_status(), self._agent_environment_problems()
        )

    def _usage_checks(self) -> dict[str, CliAgentUsageCheck]:
        return self._cli_agent_usage.checks()

    def _active_agent_ids(self) -> list[str]:
        try:
            team = self._get_context().team
        except Exception:  # pylint: disable=broad-exception-caught
            return []
        return sorted(
            member.person_id
            for member in team.members
            if member.is_active and member.person_type != "human"
        )

    def _agent_environment_problems(self) -> list[EnvironmentProblemEntry]:
        try:
            return agent_environment_problems(
                self._active_agent_ids(), usage_checks=self._usage_checks()
            )
        except Exception:  # pylint: disable=broad-exception-caught
            # A broken definition is reported where it is edited; the alert
            # band is not the place to fail.
            return []

    def get_agent_environment_status(self) -> AgentEnvironmentStatusResponse:
        """This device's agent environment, and every active member's slots on it."""
        with self._environment_build_lock:
            building = (
                self._environment_build is not None
                and self._environment_build.is_alive()
            )
            output = list(self._environment_build_output)
        return agent_environment_status(
            self._active_agent_ids(),
            build_output=output,
            building_here=building,
            usage_checks=self._usage_checks(),
        )

    def build_agent_environment(self) -> AgentEnvironmentStatusResponse:
        """Build this device's snapshot in the background and report the status.

        The build is what ``guildbotics environment build`` and the service's
        upkeep run; a build already running here or elsewhere is left to
        finish, and the status says so.

        Raises:
            AppApiError: ``agent_environment_unavailable`` when this device
                cannot run the environment, ``agent_environment_declaration``
                when the declaration cannot be read.
        """
        health = doctor()
        if not health.available:
            raise AppApiError(
                "agent_environment_unavailable", reason=health.reason, status_code=409
            )
        try:
            declaration = load_toolchain()
        except ToolchainError as exc:
            raise AppApiError(
                "agent_environment_declaration", reason=str(exc), status_code=400
            ) from exc
        with self._environment_build_lock:
            if (
                self._environment_build is None
                or not self._environment_build.is_alive()
            ):
                self._environment_build_output.clear()
                self._environment_build = threading.Thread(
                    target=self._run_environment_build,
                    args=(declaration,),
                    name="agent-environment-build",
                    daemon=True,
                )
                self._environment_build.start()
        return self.get_agent_environment_status()

    def _run_environment_build(self, declaration: ToolchainDeclaration) -> None:
        def on_line(line: str) -> None:
            with self._environment_build_lock:
                self._environment_build_output.append(line)

        # The outcome needs no event of its own: the status reports a failed
        # build with its reason, and the alert band opens on it.
        try:
            asyncio.run(build_snapshot(declaration, on_line=on_line))
        except (AgentEnvironmentError, ToolchainError) as exc:
            on_line(str(exc))
            self._event_bus.publish_log(
                "WARNING", f"The agent environment build failed: {exc}"
            )

    def dismiss_system_alert(self, alert_id: str) -> SystemAlertsResponse:
        active_ids = {alert.id for alert in self.get_system_alerts().alerts}
        if alert_id in active_ids:
            self._system_alerts.dismiss(alert_id)
        return self.get_system_alerts()

    def reset_chat_receive_state(self) -> ChatReceiveResetResponse:
        """Ignore every chat message up to now across all active Slack members.

        Records a per-member receive cutoff at the current time (a hard floor
        that backfill never fetches before, covering channels known only by name
        with no stored state yet) and drops received-but-unprocessed events, so
        the next start only handles messages that arrive afterwards. Rejected
        while the runtime is running so it never races the live listener.
        """
        status = self.get_scheduler_status()
        if status.scheduler.running or status.events.running:
            raise AppApiError(
                "runtime_running",
                status_code=409,
            )
        context = self._get_context()
        self._load_workspace_env()
        store = FileConversationStateStore()
        cutoff_ts = f"{time.time():.6f}"
        members_reset = 0
        channels_reset = 0
        for member in context.team.members:
            if not getattr(member, "is_active", False):
                continue
            if not self._has_slack_subscription(member):
                continue
            store.save_receive_cutoff("slack", member.person_id, cutoff_ts)
            for channel_id in store.list_known_channels("slack", member.person_id):
                store.clear_channel_receive_backlog(
                    "slack", member.person_id, channel_id
                )
                channels_reset += 1
            members_reset += 1
        self._event_bus.publish_event(
            "chat.receive_state_reset",
            {"members": members_reset, "channels": channels_reset},
        )
        return ChatReceiveResetResponse(
            members_reset=members_reset, channels_reset=channels_reset
        )

    def _has_slack_subscription(self, member: Any) -> bool:
        """True when a member subscribes to any enabled Slack channel, whether it
        is identified by id or only by name."""
        for sub in get_chat_subscriptions(member):
            if not isinstance(sub, dict):
                continue
            if str(sub.get("service", "slack")).strip().lower() != "slack":
                continue
            if not bool(sub.get("enabled", True)):
                continue
            channel_id = str(sub.get("channel_id", "") or "").strip()
            channel_name = str(sub.get("channel_name", "") or "").strip()
            if channel_id or channel_name:
                return True
        return False

    def get_transcript_settings(self) -> TranscriptSettingsStatus:
        usage = (
            self._diagnostics_store.transcript_usage()
            if self._diagnostics_store is not None
            else {
                "total_size_bytes": 0,
                "index_size_bytes": 0,
            }
        )
        memory_size, memory_max_size = MemoryAuditStore().usage()
        return TranscriptSettingsStatus(
            detail=cast(Any, transcript_detail()),
            retention_days=transcript_retention_days(),
            sessions_dir=get_workspace_local_path("run", "sessions"),
            total_size_bytes=int(usage["total_size_bytes"]),
            index_size_bytes=int(usage["index_size_bytes"]),
            index_rewrite_threshold_bytes=DEFAULT_DIAGNOSTICS_MAX_BYTES,
            memory_size_bytes=memory_size,
            memory_max_size_bytes=memory_max_size,
        )

    def update_transcript_settings(
        self, request: TranscriptSettingsUpdateRequest
    ) -> TranscriptSettingsStatus:
        from guildbotics.observability.session_transcripts import (
            write_transcript_settings,
        )

        config_dir = get_primary_config_dir()
        if config_dir is None:
            raise WorkspaceNotConfiguredError(
                "No workspace is selected, so there are no transcript settings."
            )
        # `config/transcripts.yml` is shared like the rest of config.
        apply_config_write(
            config_dir,
            lambda: write_transcript_settings(
                detail=request.detail, retention_days=request.retention_days
            ),
        )
        return self.get_transcript_settings()

    def get_runtime_debug_status(self) -> RuntimeDebugStatus:
        debug_values = read_debug_env()
        log_level = str(
            debug_values.get("LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
        )
        agno_debug = _env_truthy(
            str(debug_values.get("AGNO_DEBUG") or os.getenv("AGNO_DEBUG") or "")
        )
        normalized_log_level = log_level.strip().upper() or "INFO"
        return RuntimeDebugStatus(
            enabled=normalized_log_level == "DEBUG" or agno_debug,
            log_level=normalized_log_level,
            agno_debug=agno_debug,
        )

    def update_runtime_debug(
        self, request: RuntimeDebugUpdateRequest
    ) -> RuntimeDebugStatus:
        log_level = "DEBUG" if request.enabled else "INFO"
        agno_debug = "true" if request.enabled else "false"
        write_debug_env({"LOG_LEVEL": log_level, "AGNO_DEBUG": agno_debug})
        apply_debug_env_to_process({"LOG_LEVEL": log_level, "AGNO_DEBUG": agno_debug})
        _apply_runtime_log_level(log_level)
        return self.get_runtime_debug_status()

    def verify(self) -> VerifyResponse:
        with trace_scope("diagnostics", command="verify"):
            status = self.get_config_status()
            team = None
            team_error = None
            try:
                team = self._get_context().team
            except Exception as exc:
                team_error = exc

            response = VerifyService().verify(
                config=status, team=team, team_error=team_error
            )
            self._event_bus.publish_event(
                "verify.completed",
                {
                    "ok": response.ok,
                    "checks": [check.model_dump() for check in response.checks],
                },
            )
            return response

    async def run_scenario_diagnostics(
        self, person_id: str | None = None
    ) -> ScenarioDiagnosticsResponse:
        with trace_scope(
            "diagnostics", command="diagnostics", person_id=person_id or ""
        ):
            context = None
            context_error = None
            try:
                context = self._get_context()
            except Exception as exc:
                context_error = exc
            try:
                response = await ScenarioDiagnosticsService().run(
                    context=context,
                    context_error=context_error,
                    person_id=person_id,
                )
                self._event_bus.publish_event(
                    "diagnostics.completed",
                    {
                        "ok": response.ok,
                        "active_members": response.active_members,
                        "scope_person_id": person_id or "",
                        "checks": [check.model_dump() for check in response.checks],
                    },
                )
                return response
            finally:
                if context is not None:
                    await context.aclose()

    def list_traces(
        self,
        *,
        source: str | None = None,
        person_id: str | None = None,
        query: str | None = None,
        attr_key: str | None = None,
        attr_value: str | None = None,
        limit: int = 200,
    ) -> TracesResponse:
        if self._diagnostics_store is None:
            return TracesResponse(traces=[])
        summaries = self._diagnostics_store.list_traces(
            source=source,
            person_id=person_id,
            query=query,
            attr_key=attr_key,
            attr_value=attr_value,
            limit=limit,
            completion_summary=_completion_summary_lookup(),
        )
        traces = [TraceSummary.model_validate(summary) for summary in summaries]
        return TracesResponse(traces=traces)

    def get_trace_detail(self, trace_id: str) -> TraceDetailResponse:
        records: list[TraceRecord] = []
        summary = None
        if self._diagnostics_store is not None:
            raw_summary = self._diagnostics_store.get_summary(
                trace_id, _completion_summary_lookup()
            )
            summary = (
                TraceSummary.model_validate(raw_summary)
                if raw_summary is not None
                else None
            )
            records.extend(
                _to_trace_record(item)
                for item in collapse_assistant_streams(
                    self._diagnostics_store.get_records(trace_id)
                )
            )
        records.extend(self._memory_trace_records(trace_id))
        records.sort(key=_trace_record_sort_key)
        transcript_available = (
            self._diagnostics_store.transcript_exists(trace_id)
            if self._diagnostics_store is not None
            else False
        )
        return TraceDetailResponse(
            trace_id=trace_id,
            summary=summary,
            records=records,
            transcript_available=transcript_available,
        )

    async def troubleshoot(
        self, request: TroubleshootingRequest
    ) -> TroubleshootingResponse:
        """Answer one troubleshooting question about the recorded diagnostics."""
        focus = request.focus or TroubleshootingFocus()

        def message(access: CommandAccess) -> str:
            # The directories are named the way the agent's environment mounts
            # them, from what the command declares it inspects.
            directories = inspected_directories(access.inspects, get_workspace_root())
            return json.dumps(
                {
                    "question": request.message,
                    "focus": focus.model_dump(),
                    "directories": {
                        name: guest_path(path) for name, path in directories.items()
                    },
                },
                ensure_ascii=False,
            )

        trace_id, outcome = await self._execute_command(
            _Execution(
                command="assistants/troubleshoot",
                label=f"troubleshoot:{focus.trace_id or focus.view}",
                args=[f"conversation_id={request.conversation_id}"],
                cwd=partial(_assistant_cwd, "troubleshooting"),
                failure_code="troubleshooting_failed",
                failure_status=502,
                attributes={"troubleshooting.conversation_id": request.conversation_id},
                result_type=TroubleshootingResult,
            ),
            person=request.person,
            message=message,
        )
        result = cast(TroubleshootingResult, outcome.result)
        return TroubleshootingResponse(
            trace_id=trace_id,
            message=result.message,
            # An agent can name a trace it never read, and the frontend turns
            # every reference into a link, so only keep recorded ones.
            trace_ids=[
                candidate
                for candidate in result.trace_ids
                if self._trace_exists(candidate)
            ],
        )

    def _trace_exists(self, trace_id: str) -> bool:
        if not trace_id or self._diagnostics_store is None:
            return False
        return self._diagnostics_store.get_summary(trace_id) is not None

    def get_global_records(self, limit: int = 200) -> TraceDetailResponse:
        records: list[TraceRecord] = []
        trace_id = ""
        summary = None
        if self._diagnostics_store is not None:
            trace_id = self._diagnostics_store.latest_system_trace_id() or ""
            if trace_id:
                raw_summary = self._diagnostics_store.get_summary(trace_id)
                summary = (
                    TraceSummary.model_validate(raw_summary)
                    if raw_summary is not None
                    else None
                )
            records.extend(
                _to_trace_record(item)
                for item in self._diagnostics_store.global_records(limit=limit)
            )
        return TraceDetailResponse(
            trace_id=trace_id,
            summary=summary,
            records=records,
            transcript_available=bool(records),
        )

    def get_activity_history(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        refresh: bool = False,
        sync_start: str | None = None,
        sync_end: str | None = None,
    ) -> ActivityHistoryResponse:
        end_time = parse_timestamp(end or "") or datetime.now(UTC)
        start_time = parse_timestamp(start or "") or (end_time - timedelta(days=7))
        if start_time > end_time:
            raise AppApiError(
                "invalid_activity_history_range",
                context={"start": start or "", "end": end or ""},
                status_code=400,
            )
        if (sync_start is None) != (sync_end is None):
            raise AppApiError(
                "invalid_activity_sync_range",
                "invalid_activity_sync_range.together",
                context={"sync_start": sync_start or "", "sync_end": sync_end or ""},
                status_code=400,
            )
        if sync_start is None:
            sync_start_time = start_time
            sync_end_time = end_time
        else:
            parsed_sync_start = parse_timestamp(sync_start)
            parsed_sync_end = parse_timestamp(sync_end or "")
            if parsed_sync_start is None or parsed_sync_end is None:
                raise AppApiError(
                    "invalid_activity_sync_range",
                    "invalid_activity_sync_range.timestamps",
                    context={
                        "sync_start": sync_start or "",
                        "sync_end": sync_end or "",
                    },
                    status_code=400,
                )
            sync_start_time = parsed_sync_start
            sync_end_time = parsed_sync_end
            if sync_start_time > sync_end_time:
                raise AppApiError(
                    "invalid_activity_sync_range",
                    "invalid_activity_sync_range.order",
                    context={
                        "sync_start": sync_start or "",
                        "sync_end": sync_end or "",
                    },
                    status_code=400,
                )
        context = self._get_context()
        self._refresh_activity_events(
            context.team, sync_start_time, sync_end_time, force=refresh
        )
        return build_activity_history(
            start=start_time,
            end=end_time,
            members=context.team.members,
            lifecycles=_activity_lifecycles_between(start_time, end_time),
            records=self._activity_records_between(start_time, end_time),
            detail_available=(
                self._diagnostics_store.transcript_exists
                if self._diagnostics_store is not None
                else lambda _trace_id: False
            ),
        )

    def _refresh_activity_events(
        self, team: Team, start: datetime, end: datetime, *, force: bool
    ) -> None:
        """Refresh GitHub-backed shared activity at most once per five minutes."""
        with self._activity_sync_lock:
            completed_weeks = _completed_activity_weeks()
            period = (start.isoformat(), end.isoformat())
            if not force and end <= datetime.now(UTC) and period in completed_weeks:
                return
            now = time.monotonic()
            last_attempt = self._activity_sync_attempts.get(period)
            if (
                not force
                and last_attempt is not None
                and now - last_attempt < ACTIVITY_SYNC_COOLDOWN_SECONDS
            ):
                return
            # Count failed attempts too: a bad credential must not turn the UI's
            # five-second history refresh into a five-second GitHub retry loop.
            self._activity_sync_attempts[period] = now
            threading.Thread(
                target=self._sync_activity_events,
                args=(team, start, end, period),
                daemon=True,
            ).start()

    def _sync_activity_events(
        self, team: Team, start: datetime, end: datetime, period: tuple[str, str]
    ) -> None:
        try:
            asyncio.run(refresh_github_activity_events(team, start, end))
            if end <= datetime.now(UTC):
                _mark_activity_week_completed(period)
        except Exception as exc:
            self._event_bus.publish_log(
                "WARNING", f"GitHub activity refresh failed: {exc}"
            )

    def _activity_records_between(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """The fact records of the window: what happened inside each execution.

        Executions themselves come from their lifecycle records
        (:func:`_activity_lifecycles_between`), so the only local diagnostics
        read here are the work targets member commands declared: they title a
        session on the device that ran it and are nowhere else. Every record
        keeps the trace id it was recorded with, whichever device recorded it:
        the facts of an execution belong to its session on every device, and
        whether *this* device can show the execution's detail is a separate
        question (``ActivityHistorySession.detail_available``).
        """

        def includes(value: str) -> bool:
            return _timestamp_in_range(value, start, end)

        records = ActivityEventStore().records_between(start, end)
        if self._diagnostics_store is not None:
            records.extend(
                item
                for item in self._diagnostics_store.records_between(includes=includes)
                if item.get("type") == GITHUB_WORK_TARGET_EVENT_TYPE
            )
        records.extend(
            MemoryAuditStore().list_events(
                since=start.isoformat(), until=end.isoformat()
            )
        )
        return records

    def list_memory_events(
        self,
        *,
        person_id: str | None = None,
        doc_id: str | None = None,
        action: str | None = None,
        trace_id: str | None = None,
        source: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> MemoryEventsResponse:
        filters = {
            "person_id": person_id,
            "doc_id": doc_id,
            "action": action,
            "trace_id": trace_id,
            "source": source,
            "query": query,
            "since": since,
            "until": until,
        }
        raw_events = MemoryAuditStore().list_events(**filters, limit=limit)
        return MemoryEventsResponse(
            event_count=len(raw_events),
            events=[_memory_event(item) for item in raw_events],
        )

    def _memory_trace_records(self, trace_id: str) -> list[TraceRecord]:
        return [
            _to_trace_record(item)
            for item in MemoryAuditStore().list_events(trace_id=trace_id, limit=1000)
        ]

    async def fetch_project_status_options(
        self, request: ProjectStatusOptionsRequest
    ) -> ProjectStatusOptionsResponse:
        """Read the Status options of the GitHub Project identified by *request*.

        Reads live (no writes) using a configured member's GitHub credentials,
        so the setup form can list lanes for the project URL being entered
        before it is saved. Returns ``available=False`` (instead of raising)
        whenever options cannot be read—incomplete identity, no member token,
        or a GitHub error—so the form falls back to manual lane entry.
        """
        result = await self._with_setup_ticket_manager(
            request, lambda tm: tm.get_statuses()
        )
        if result is None:
            return ProjectStatusOptionsResponse(available=False)
        return ProjectStatusOptionsResponse(available=True, statuses=result)

    async def fetch_agent_field_state(
        self, request: ProjectStatusOptionsRequest
    ) -> AgentFieldStateResponse:
        """Read the ``Agent`` field state of the GitHub Project in *request*.

        Read-only. Uses a configured member's credentials (like
        :meth:`fetch_project_status_options`) and reports the registered and
        still-missing non-human members so the setup form can show them.
        """
        result = await self._with_setup_ticket_manager(
            request, lambda tm: tm.get_agent_field_state()
        )
        return self._to_agent_field_response(result)

    async def ensure_agent_field(
        self, request: ProjectStatusOptionsRequest
    ) -> AgentFieldStateResponse:
        """Create the ``Agent`` field or add missing non-human-member options.

        Existing options are preserved (resubmitted with their ids) so ticket
        assignments are never cleared. Returns the refreshed field state.
        """
        result = await self._with_setup_ticket_manager(
            request, lambda tm: tm.sync_agent_field()
        )
        return self._to_agent_field_response(result)

    @staticmethod
    def _to_agent_field_response(
        result: dict[str, Any] | None,
    ) -> AgentFieldStateResponse:
        if result is None:
            return AgentFieldStateResponse(available=False)
        return AgentFieldStateResponse(
            available=True,
            exists=bool(result["exists"]),
            options=[AgentFieldOption(**opt) for opt in result["options"]],
            missing=[AgentFieldOption(**opt) for opt in result["missing"]],
        )

    async def _with_setup_ticket_manager(
        self,
        request: ProjectStatusOptionsRequest,
        action: Callable[[GitHubTicketManager], Awaitable[Any]],
    ) -> Any:
        """Run *action* against a GitHubTicketManager built from form identity.

        The project identity comes from the (possibly unsaved) form, while the
        member roster and credentials come from the saved team config. Tries
        each member's credentials until one succeeds; returns the action result,
        or ``None`` when the project identity is incomplete, no context/member is
        available, or every attempt fails (so callers degrade gracefully).
        """
        if not (request.owner and request.project_id and request.github_project_url):
            return None
        try:
            context = self._get_context()
        except Exception:
            return None
        try:
            members = [m for m in context.team.members if m.is_active]
            members = members or list(context.team.members)
            project = Project(
                name=context.team.project.name or "setup",
                services={
                    "ticket_manager": {
                        "name": "GitHub",
                        "owner": request.owner,
                        "project_id": request.project_id,
                        "url": request.github_project_url,
                    }
                },
            )
            team = Team(project=project, members=context.team.members)
            logger = logging.getLogger("guildbotics.app_api.setup_github")
            for member in members:
                # Construct inside the try: GitHubTicketManager.__init__ raises for
                # a member without a GitHub username, and such members must be
                # skipped (not surfaced as a 500) so a later credentialed member
                # is still tried.
                ticket_manager: GitHubTicketManager | None = None
                try:
                    ticket_manager = GitHubTicketManager(logger, member, team)
                    return await action(ticket_manager)
                except Exception:
                    continue
                finally:
                    if ticket_manager is not None and ticket_manager.client is not None:
                        await ticket_manager.client.aclose()
            return None
        finally:
            await context.aclose()

    async def get_cli_agent_usage(
        self, agent_name: str, refresh: bool = False
    ) -> CliAgentUsageResponse:
        """Return one AI CLI tool's account usage (see :class:`CliAgentUsageCache`)."""
        return await self._cli_agent_usage.read(agent_name, refresh)

    async def close_cli_agent_usage(self) -> None:
        """Cancel running usage probes so their environments are released."""
        await self._cli_agent_usage.aclose()

    def is_github_integration_enabled(self) -> bool:
        try:
            return self._get_context().team.project.is_available_service(
                Service.TICKET_MANAGER
            )
        except Exception:
            return False

    def _get_context(self, message: str = "") -> Context:
        self._load_workspace_env()
        try:
            return get_edition().get_context(message)
        except FileNotFoundError as exc:
            raise AppApiError(
                "config_not_found",
                context={"path": str(exc.filename or "")},
            ) from exc

    def _load_workspace_env(self) -> None:
        """Load debug settings and keychain secrets for the selected workspace.

        The workspace itself is selected only by ``set_workspace()`` or the
        launcher's restored active workspace — never derived from the process
        cwd. Without a selection there is nothing to load.
        """
        try:
            workspace = get_workspace_root()
        except WorkspaceNotConfiguredError:
            return
        apply_workspace_root(workspace)
        new_values: dict[str, str] = {}
        new_values.update(read_debug_env())
        # Real environment variables (not injected by this runtime) win over
        # keychain values and are not even fetched from the keychain.
        new_values.update(
            read_workspace_secrets(
                skip=frozenset(set(os.environ) - self._loaded_dotenv_keys)
            )
        )
        loaded_keys = set(new_values) - WORKSPACE_DOTENV_PROTECTED_KEYS
        for key in self._loaded_dotenv_keys - loaded_keys:
            os.environ.pop(key, None)
        for key in loaded_keys:
            if key in self._loaded_dotenv_keys or key not in os.environ:
                os.environ[key] = new_values[key]
        self._loaded_dotenv_keys = loaded_keys

    def _reserve_command(
        self,
        trace_id: str,
        expected_workspace: Path | None = None,
        *,
        exclusive: bool = True,
    ) -> None:
        """Accept a command run for the selected workspace.

        The workspace cannot switch until the run is released. An exclusive
        run also takes the one manual-command slot; a read-only one does not,
        since its turns can change nothing.

        Args:
            trace_id: The run being accepted.
            expected_workspace: The workspace the command was asked for.
            exclusive: Whether the run takes the manual-command slot.

        Raises:
            AppApiError: If the workspace is switching or is not the expected
                one, or another run holds the slot.
        """
        # Workspace switching may wait for I/O while holding this lock. A
        # command arrives on the event loop, so reject it instead of blocking.
        if not self._lock.acquire(blocking=False):
            raise AppApiError("command_workspace_changing", status_code=409)
        try:
            if (
                expected_workspace is not None
                and self.get_config_status().workspace != expected_workspace.resolve()
            ):
                raise AppApiError("command_workspace_changed", status_code=409)
            if exclusive:
                if self._running_command_id not in (None, trace_id):
                    raise AppApiError(
                        "command_already_running",
                        status_code=409,
                        context={"trace_id": self._running_command_id},
                    )
                self._running_command_id = trace_id
            self._accepted_command_ids.add(trace_id)
        finally:
            self._lock.release()

    def _release_command(self, trace_id: str) -> None:
        with self._lock:
            self._accepted_command_ids.discard(trace_id)
            if self._running_command_id == trace_id:
                self._running_command_id = None


def _command_roots(person_id: str) -> list[Path]:
    """Physical roots whose logical command names seed the general catalog."""
    try:
        primary = get_primary_config_path(Path())
    except WorkspaceNotConfiguredError:
        return []
    return [
        primary / "team" / "members" / person_id / "commands",
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


def _is_routine_command(metadata: dict[str, Any]) -> bool:
    return metadata.get("routine") is True


def _default_routine_command(options: list[CommandOption]) -> str:
    """Pick the routine command to seed / pre-select for a new member.

    A single eligible candidate is the default on its own; with several, the
    edition's declared default wins (``workflows/ticket_driven_workflow`` for the
    simple edition), so the literal name lives only in the edition.
    """
    eligible = [option.command for option in options if option.routine_eligible]
    if len(eligible) == 1:
        return eligible[0]
    for command in get_edition().get_default_routines():
        if command in eligible:
            return command
    return eligible[0] if eligible else ""


def _timestamp_in_range(value: str, start: datetime, end: datetime) -> bool:
    parsed = parse_timestamp(value)
    return parsed is not None and start <= parsed < end


def _command_option(
    *,
    command: str,
    path: Path,
    github_enabled: bool,
    context: Context,
    metadata: dict[str, Any] | None = None,
) -> CommandOption:
    if metadata is None:
        metadata = load_command_metadata(path, context.team.project.get_language_code())
    requirements = _command_requirements(path, metadata, github_enabled, context)
    description = str(metadata.get("description", ""))
    try:
        arguments = to_command_arguments(parse_command_arguments(path, metadata))
    except CommandError:
        arguments = []
    try:
        inputs = to_command_inputs(parse_command_input_policy(metadata.get("inputs")))
    except CommandError:
        inputs = to_command_inputs(parse_command_input_policy(None))
    return CommandOption(
        command=command,
        label=str(metadata.get("name") or default_command_label(command)),
        description=description,
        category=cast(Any, _command_category(command)),
        source=cast(Any, command_source(path)),
        path=path,
        arguments=arguments,
        inputs=inputs,
        requirements=requirements,
    )


def _command_category(command: str) -> str:
    if command.startswith("workflows/"):
        return "workflow"
    if command.startswith("functions/"):
        return "function"
    if command.startswith("examples/"):
        return "example"
    return "custom"


def _shadow_source(resolved: Path | None) -> str:
    """Classify why a resolved command differs from the selected shared file."""
    if resolved is None:
        return "workspace"
    if is_within(resolved, get_template_path()):
        return "template"
    if is_within(resolved, get_shared_commands_root()):
        return "workspace"
    return "member"


def _resolved_display(resolved: Path) -> str:
    for root in (get_shared_commands_root(), get_template_path()):
        if is_within(resolved, root):
            return (
                resolved.resolve(strict=False)
                .relative_to(root.resolve(strict=False))
                .as_posix()
            )
    return resolved.name


def _command_requirements(
    path: Path,
    metadata: dict[str, Any],
    github_enabled: bool,
    context: Context,
) -> list[CommandRequirement]:
    kinds = _command_requirement_kinds(path, metadata, context, set())
    return [
        CommandRequirement(
            kind=cast(Any, kind),
            satisfied=_requirement_satisfied(kind, github_enabled),
            message=_requirement_message(kind),
        )
        for kind in sorted(kinds)
    ]


def _command_requirement_kinds(
    path: Path,
    metadata: dict[str, Any],
    context: Context,
    seen: set[Path],
) -> set[str]:
    resolved_path = path.resolve(strict=False)
    if resolved_path in seen:
        return set()
    seen.add(resolved_path)

    kinds: set[str] = _direct_command_requirement_kinds(path, metadata, context)
    kinds.update(_child_command_requirement_kinds(path, metadata, context, seen))
    return kinds


def _direct_command_requirement_kinds(
    path: Path, metadata: dict[str, Any], context: Context
) -> set[str]:
    if path.suffix == ".md":
        kind = _markdown_brain_requirement_kind(metadata, context)
        if kind:
            return {kind}
        return set()
    if path.suffix == ".py":
        return _python_requirement_kinds(path)
    return set()


def _markdown_brain_requirement_kind(
    metadata: dict[str, Any], context: Context
) -> str | None:
    return _brain_requirement_kind(metadata.get("brain", "default"), context)


def _brain_requirement_kind(brain_value: object, context: Context) -> str | None:
    brain = str(brain_value).strip()
    if is_brain_disabled(brain):
        return None

    try:
        mapping = load_yaml_file(
            get_person_config_path(
                context.person.person_id, "intelligences/brain_mapping.yml"
            )
        )
    except Exception:
        mapping = {}
    brain_config = mapping.get(brain, {}) if isinstance(mapping, dict) else {}
    if isinstance(brain_config, dict) and brain_config.get("class") == CLI_BRAIN_CLASS:
        return "cli_agent"
    return "llm"


def _child_command_requirement_kinds(
    path: Path,
    metadata: dict[str, Any],
    context: Context,
    seen: set[Path],
) -> set[str]:
    raw_commands = metadata.get("commands")
    if raw_commands is None:
        return set()
    entries = raw_commands if isinstance(raw_commands, list) else [raw_commands]
    kinds: set[str] = set()
    for entry in entries:
        kinds.update(
            _command_entry_requirement_kinds(path.parent, entry, context, seen)
        )
    return kinds


def _command_entry_requirement_kinds(
    base_dir: Path,
    entry: object,
    context: Context,
    seen: set[Path],
) -> set[str]:
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


def _inline_markdown_requirement_kinds(entry: dict, context: Context) -> set[str]:
    if "print" in entry:
        return set()
    kind = _brain_requirement_kind(entry.get("brain", "default"), context)
    if kind is None:
        return set()
    return {kind}


def _inline_python_requirement_kinds(entry: dict) -> set[str]:
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
) -> set[str]:
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


def _python_requirement_kinds(path: Path) -> set[str]:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return _python_module_requirement_kinds(module)


def _python_module_requirement_kinds(module: ast.Module) -> set[str]:
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

    kinds: set[str] = set()
    if (
        modules & {"guildbotics.integrations.ticket_manager"}
        or names
        & {
            "TicketManager",
            "GitHubTicketManager",
        }
        or attrs & {"get_ticket_manager", "create_ticket_manager"}
    ):
        kinds.add("github")
    if (
        modules & {"guildbotics.integrations.chat_service"}
        or names
        & {
            "ChatService",
        }
        or attrs & {"get_chat_service", "create_chat_service"}
    ):
        kinds.add("slack")
    if modules & {"guildbotics.intelligences.functions"} or attrs & {"get_brain"}:
        kinds.add("llm")
    if names & {"CliAgentBrain"}:
        kinds.add("cli_agent")
    return kinds


def _requirement_satisfied(kind: str, github_enabled: bool) -> bool:
    from guildbotics.utils.fileio import get_config_path

    if kind == "github":
        return github_enabled
    if kind == "slack":
        return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APP_TOKEN"))
    if kind == "llm":
        from guildbotics.intelligences.llm_providers import provider_env_keys

        return any(
            os.getenv(env_var)
            for env_var in provider_env_keys(get_config_path("")).values()
        )
    if kind == "cli_agent":
        return any(resolve_cli_agent_path(agent.executable) for agent in CLI_AGENTS)
    return True


def _requirement_message(kind: str) -> str:
    return {
        "github": "GitHub integration is required.",
        "slack": "Slack bot and app tokens are required.",
        "llm": "An LLM API key is required.",
        "cli_agent": "A configured AI CLI tool executable is required.",
    }.get(kind, "")


def _env_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _apply_runtime_log_level(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger("guildbotics")
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)

    try:
        from agno.utils import log as agno_log
    except Exception:
        return

    agno_log.logger.setLevel(level)
    for handler in agno_log.logger.handlers:
        handler.setLevel(level)


def _activity_lifecycles_between(
    start: datetime, end: datetime
) -> list[ActivityLifecycle]:
    """One lifecycle per execution active in the window, from its shared record."""
    lifecycles = [
        lifecycle_from_run(record) for record in RunStore().records_between(start, end)
    ]
    lifecycles.extend(
        lifecycle_from_session(record)
        for record in InteractiveSessionStore().list_between(start, end)
    )
    return lifecycles


def _completion_summary_lookup() -> CompletionSummary:
    """Resolve a trace's recorded run completion summary from its attributes.

    The run records are read once per request, and only if some trace asks
    (``resolve_trace_title`` asks only for a trace that names no PR / issue),
    so listing traces stays cheap.
    """
    store = RunStore()
    summaries: dict[tuple[str, str], str] | None = None

    def lookup(attributes: Mapping[str, Any], person_id: str) -> str:
        nonlocal summaries
        if summaries is None:
            summaries = store.summaries_by_subject()
        return summaries.get((run_subject_id(attributes), person_id), "")

    return lookup


def _to_trace_record(item: dict[str, Any]) -> TraceRecord:
    attributes = item.get("attributes")
    payload = item.get("payload")
    return TraceRecord(
        kind=str(item.get("kind", "")),
        timestamp=str(item.get("timestamp", "")),
        trace_id=item.get("trace_id"),
        span_id=item.get("span_id"),
        parent_id=item.get("parent_id"),
        call_id=item.get("call_id"),
        span=str(item.get("span") or ""),
        source=str(item.get("source") or ""),
        person_id=str(item.get("person_id") or ""),
        command=str(item.get("command") or ""),
        workflow=str(item.get("workflow") or ""),
        type=str(item.get("type") or ""),
        level=str(item.get("level") or ""),
        message=str(item.get("message") or ""),
        attributes=attributes if isinstance(attributes, dict) else {},
        payload=payload if isinstance(payload, dict) else {},
        presentation=normalize_trace_presentation(item),
    )


def _trace_record_sort_key(record: TraceRecord) -> datetime:
    return parse_memory_audit_timestamp(record.timestamp) or datetime.min.replace(
        tzinfo=UTC
    )


def _memory_event(item: dict[str, Any]) -> MemoryEvent:
    raw_attributes = item.get("attributes")
    raw_payload = item.get("payload")
    attributes = (
        cast(dict[str, Any], raw_attributes) if isinstance(raw_attributes, dict) else {}
    )
    payload = cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
    source = payload.get("source")
    changed_fields = payload.get("changed_fields")
    query_keywords = payload.get("query_keywords")
    path = str(attributes.get("memory.path") or "")
    action = str(
        attributes.get("memory.action")
        or str(item.get("type") or "").removeprefix("memory.")
    )
    return MemoryEvent(
        timestamp=str(item.get("timestamp") or ""),
        action=action,
        person_id=str(item.get("person_id") or ""),
        scope=str(attributes.get("memory.scope") or ""),
        doc_id=str(attributes.get("memory.doc_id") or ""),
        path=path,
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        kind=str(attributes.get("memory.kind") or ""),
        trace_id=item.get("trace_id"),
        run_id=str(attributes.get("run_id") or ""),
        task_run_id=str(attributes.get("task_run_id") or ""),
        source=[entry for entry in source if isinstance(entry, dict)]
        if isinstance(source, list)
        else [],
        changed_fields=[
            str(field) for field in changed_fields if isinstance(field, str)
        ]
        if isinstance(changed_fields, list)
        else [],
        query_keywords=[
            str(keyword) for keyword in query_keywords if isinstance(keyword, str)
        ]
        if isinstance(query_keywords, list)
        else [],
        result_count=_optional_int(payload.get("result_count")),
        duration_ms=_optional_float(payload.get("duration_ms")),
        body_preview=_memory_body_preview(path),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _memory_body_preview(path: str, *, limit: int = 800) -> str:
    parts = path.split("/")
    if len(parts) < MIN_MEMORY_DOCUMENT_PATH_PARTS or parts[0] != "documents":
        return ""
    relative_parts = parts[1:]
    if any(part in {"", ".", ".."} for part in relative_parts):
        return ""
    body_path = get_workspace_state_path("documents", *relative_parts) / "body.md"
    if not body_path.is_file():
        return ""
    try:
        body = body_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return body[:limit]


def _workspace_switch_blocked_error(status: RuntimeStatus) -> AppApiError:
    return AppApiError(
        "workspace_switch_blocked_by_active_work",
        context={
            "active_work_count": len(status.active_works),
            "scheduler_state": status.scheduler.state,
            "events_state": status.events.state,
        },
        status_code=409,
    )


def _default_command_cwd() -> Path:
    """Where a command runs when the screen names no directory: the exchange
    directory, so what it produces lands where the user looks for it."""
    cwd = exchange_dir()
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def _assistant_cwd(name: str) -> Path:
    """Where a Desktop assistant's turns work: its own directory under
    ``.guildbotics/local/work``."""
    cwd = get_workspace_work_path(name, workspace_root=get_workspace_root())
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd
