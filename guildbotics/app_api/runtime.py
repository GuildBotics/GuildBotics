from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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
)
from guildbotics.app_api.agent_environment_status import (
    EnvironmentProblemEntry,
    agent_environment_problems,
    agent_environment_status,
)
from guildbotics.app_api.cli_agent_usage import CliAgentUsageCache
from guildbotics.app_api.command_catalog import (
    command_file_execution_status,
    command_options,
    guard_run_target,
    routine_command_options,
)
from guildbotics.app_api.command_files import CommandFileService
from guildbotics.app_api.command_input_files import command_cwd
from guildbotics.app_api.config_revisions import apply_config_write
from guildbotics.app_api.diagnostics import ScenarioDiagnosticsService
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.lifecycle import RuntimeLifecycleService
from guildbotics.app_api.models import (
    ActivityHistoryResponse,
    AgentEnvironmentStatusResponse,
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
    CommandOptionsResponse,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    MemberSummary,
    ProjectSummary,
    RoutineCommandOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    ServiceKeepAwake,
    SystemAlertsResponse,
    TeamSummary,
    TranscriptSettingsStatus,
    TranscriptSettingsUpdateRequest,
    TroubleshootingFocus,
    TroubleshootingRequest,
    TroubleshootingResponse,
    VerifyResponse,
)
from guildbotics.app_api.system_alerts import SystemAlertService
from guildbotics.app_api.verify import VerifyService
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.capabilities.activity_events import refresh_activity_events
from guildbotics.capabilities.command_failures import (
    command_failure_payload,
)
from guildbotics.capabilities.member_memory_audit import (
    MemoryAuditStore,
)
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.cli.member import run_in_process
from guildbotics.commands.authoring import CommandAuthoringResult
from guildbotics.commands.errors import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.commands.formats import EXTENSION_BY_FORMAT
from guildbotics.commands.models import CommandOutcome
from guildbotics.drivers.command_runner import (
    PreparedCommand,
    host_command_cwd,
    prepare_command,
    run_main_command,
)
from guildbotics.drivers.context import create_context
from guildbotics.drivers.execution import (
    ExecutionStatusPublisher,
    TaskRunCoordinator,
    WorkRejectedError,
)
from guildbotics.drivers.live_state import LiveStatePort
from guildbotics.drivers.member_context import resolve_person
from guildbotics.drivers.service_lock import (
    ServiceLockUnavailableError,
    service_keeps_awake,
    set_service_keeps_awake,
)
from guildbotics.entities import Person, Project, Team
from guildbotics.environment.command_environment import inspected_directories
from guildbotics.environment.contract import (
    AccessContractError,
    exchange_dir,
    validate_workspace_location,
)
from guildbotics.environment.member_broker import install_member_cli
from guildbotics.environment.runtime import (
    AgentEnvironmentError,
    doctor,
)
from guildbotics.environment.snapshot import build_snapshot
from guildbotics.environment.toolchain import (
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
)
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.factory import chat_provider_name, configured_providers
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
)
from guildbotics.intelligences.agent_runtime.wire import (
    CommandAccess,
)
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import DEFAULT_DIAGNOSTICS_MAX_BYTES
from guildbotics.observability.event_types import TARGET_RECORDED_EVENT_TYPE
from guildbotics.observability.interactive_sessions import InteractiveSessionStore
from guildbotics.observability.session_transcripts import (
    transcript_detail,
    transcript_retention_days,
)
from guildbotics.runtime import Context
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.port import ServiceError
from guildbotics.setup.setup_service import (
    PersonConfigSummary,
    SimplePersonSetupService,
)
from guildbotics.utils.correlation import new_id, trace_scope
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
    get_identity_root,
    get_machine_state_root,
    get_primary_config_dir,
    get_workspace_local_path,
    get_workspace_root,
    get_workspace_work_path,
    load_yaml_file,
)
from guildbotics.utils.workspace_state import write_active_workspace

WORKSPACE_DOTENV_PROTECTED_KEYS = {
    GUILDBOTICS_WORKSPACE_ROOT,
    *HOME_ENV_PROTECTED_KEYS,
}
ACTIVITY_SYNC_COOLDOWN_SECONDS = 5 * 60
ACTIVITY_SYNC_STATE_FILE = "activity_sync_weeks.json"
ACTIVITY_SYNC_PERIOD_PARTS = 2


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
    except OSError, ValueError, TypeError:
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
    #: The work the run does, when the Desktop names it: an assistant's
    #: conversation, whose session each of its runs resumes.
    work: Work | None = None

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
        load_workspace_environment: bool = False,
        workspace_problem: str = "",
    ) -> None:
        install_member_cli(run_in_process)
        self.event_bus = event_bus
        self.workspace_problem = workspace_problem
        self._diagnostics_store = event_bus.store
        self._system_service_run_id = new_id()
        self._system_alerts = SystemAlertService(event_bus.store)
        self._lock = threading.Lock()
        self._activity_sync_lock = threading.Lock()
        self._activity_sync_attempts: dict[tuple[str, str], float] = {}
        self._running_command_id: str | None = None
        #: Every accepted manual run, read-only ones included: the workspace
        #: does not switch under any of them.
        self._accepted_command_ids: set[str] = set()
        self.on_workspace_changed: Callable[[Path], None] | None = None
        #: The directory of the files the Desktop hands its commands, which
        #: they read; none when this runtime hands over none.
        self.command_inputs: Callable[[], Path | None] = lambda: None
        self._execution_status = ExecutionStatusPublisher()
        self._execution = TaskRunCoordinator(self._execution_status)
        self._cli_agent_usage = CliAgentUsageCache(
            lambda: self._diagnostics_store.latest_system_trace_id() or ""
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

    @property
    def selected_workspace(self) -> Path | None:
        """The selected name, without opening potentially obsolete files."""
        try:
            return get_workspace_root()
        except WorkspaceNotConfiguredError:
            return None

    def get_config_status(self) -> ConfigStatus:
        workspace = self.selected_workspace
        config_dir = get_primary_config_dir() or (
            workspace / ".guildbotics" / "config" if workspace is not None else None
        )
        project_file = (
            config_dir / "team" / "project.yml" if config_dir is not None else None
        )
        return ConfigStatus(
            workspace_problem=self.workspace_problem,
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
        try:
            workspace = validate_workspace_location(workspace_dir)
        except AccessContractError as exc:
            if isinstance(exc.__cause__, NotADirectoryError):
                raise AppApiError(
                    "workspace_not_directory",
                    context={"workspace_dir": str(workspace_dir)},
                    status_code=400,
                ) from exc
            raise
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
        self._diagnostics_store.start_system_session(self._system_service_run_id)
        self._diagnostics_store.start_maintenance()
        self._workspace_sync.activate()
        self.workspace_problem = ""
        return self.get_config_status()

    def get_team_summary(self) -> TeamSummary:
        from guildbotics.utils.i18n_tool import t
        from guildbotics.utils.person_id import (
            IDENTITY_FILE,
            MemberConfigError,
            identity_members_dir,
        )

        members: Sequence[Person | PersonConfigSummary]
        problem = ""
        try:
            context = self._get_context()
        except (AppApiError, MemberConfigError) as exc:
            status = self.get_config_status()
            if status.project_file_exists and not isinstance(exc, MemberConfigError):
                raise
            problem = str(exc) if isinstance(exc, MemberConfigError) else ""
            project = (
                Project.model_validate(load_yaml_file(status.project_file))
                if problem and status.project_file is not None
                else Project()
            )
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
            # A member whose identity this machine does not have is left out of
            # the team, and is listed here so that it can be taken out.
            waiting = [
                member
                for member in SimplePersonSetupService().list_person_configs(
                    config_dir=self.get_config_status().config_dir or Path()
                )
                if not member.identity
            ]
            members = [*context.team.members, *waiting]
            problem = "\n".join(
                t(
                    "member_config.identity_missing",
                    path=identity_members_dir() / member.person_id / IDENTITY_FILE,
                )
                for member in waiting
            )
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
            problem=problem,
        )

    def get_command_options(self, person: str | None = None) -> CommandOptionsResponse:
        return command_options(self._get_context(), person)

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
                cwd=partial(_assistant_cwd, "command-authoring"),
                failure_code="command_authoring_failed",
                failure_status=502,
                attributes={
                    "command_authoring.conversation_id": request.conversation_id
                },
                result_type=CommandAuthoringResult,
                work=Work("command_authoring", request.conversation_id),
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
        return command_file_execution_status(
            self._get_context(), file_id, person, expected_revision
        )

    def get_routine_command_options(
        self, person: str | None = None
    ) -> RoutineCommandOptionsResponse:
        return routine_command_options(self._get_context(), person)

    async def run_command(self, request: CommandRunRequest) -> CommandRunResponse:
        trace_id, outcome = await self._execute_command(
            _Execution(
                command=request.command,
                label=request.command,
                args=request.args,
                cwd=lambda: command_cwd(request.cwd) or host_command_cwd(),
                failure_code="command_error",
            ),
            person=request.person,
            message=lambda _access: request.message,
            expected_workspace=request.expected_workspace,
            guard=(
                partial(guard_run_target, request)
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
                runner = replace(
                    prepare_command(
                        context,
                        execution.command,
                        execution.args,
                        acting.person_id,
                        execution.cwd(),
                    ),
                    result_type=execution.result_type,
                    inputs=self.command_inputs(),
                    work=execution.work,
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
            self.event_bus.publish_event(
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
            self.event_bus.publish_event(
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
        self, execution: _Execution, runner: PreparedCommand
    ) -> CommandOutcome:
        # This opens the run's trace, so it is the only layer that can say the
        # whole run started and ended. Events carry the resolved person so an
        # omitted request person still shows the member the run belongs to.
        person_id = runner.context.person.person_id
        self.event_bus.publish_event(
            "command.started", {"command": execution.label, "person": person_id}
        )
        try:
            # Closing the run's context is part of the run: a close that fails
            # ends the run as failed rather than leaving it unended.
            try:
                outcome = await run_main_command(runner, source="manual")
            finally:
                await runner.context.aclose()
        except BaseException as exc:
            # Cancellation lands here as well: a force stop cancels this task,
            # and the run it started has to be reported as ended either way.
            # Anything but a failed command is a defect: it stays a generic 500,
            # so internal wording never reaches the client.
            failed = isinstance(exc, CommandError | CliAgentExecutionError)
            self.event_bus.publish_event(
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
        self.event_bus.publish_event(
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

    def get_service_keep_awake(self) -> ServiceKeepAwake:
        return ServiceKeepAwake(enabled=service_keeps_awake())

    def update_service_keep_awake(self, request: ServiceKeepAwake) -> ServiceKeepAwake:
        # The process holding the service, this one or a CLI, follows the file.
        set_service_keeps_awake(request.enabled)
        return self.get_service_keep_awake()

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
                    name="guildbotics-agent-environment-build",
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
            self.event_bus.publish_log(
                "WARNING", f"The agent environment build failed: {exc}"
            )

    def dismiss_system_alert(self, alert_id: str) -> SystemAlertsResponse:
        active_ids = {alert.id for alert in self.get_system_alerts().alerts}
        if alert_id in active_ids:
            self._system_alerts.dismiss(alert_id)
        return self.get_system_alerts()

    def reset_chat_receive_state(self) -> ChatReceiveResetResponse:
        """Ignore every chat message up to now across all subscribed members.

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
        cutoff = datetime.now(UTC)
        members_reset = 0
        channels_reset = 0
        try:
            service = chat_provider_name(context.team)
        except ServiceError:
            service = ""
        for member in context.team.members if service else []:
            if not getattr(member, "is_active", False):
                continue
            if not any(
                sub["channel_id"] or sub["channel_name"]
                for sub in get_chat_subscriptions(member)
            ):
                continue
            store.save_receive_cutoff(service, member.person_id, cutoff)
            for channel_id in store.list_known_channels(service, member.person_id):
                store.clear_channel_receive_backlog(
                    service, member.person_id, channel_id
                )
                channels_reset += 1
            members_reset += 1
        self.event_bus.publish_event(
            "chat.receive_state_reset",
            {"members": members_reset, "channels": channels_reset},
        )
        return ChatReceiveResetResponse(
            members_reset=members_reset, channels_reset=channels_reset
        )

    def get_transcript_settings(self) -> TranscriptSettingsStatus:
        usage = self._diagnostics_store.transcript_usage()
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
            self.event_bus.publish_event(
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
                self.event_bus.publish_event(
                    "diagnostics.completed",
                    {
                        "ok": response.ok,
                        "active_members": response.active_members,
                        "scope_person_id": person_id or "",
                        "checks": [check.model_dump() for check in response.checks],
                        # What the alerts of a provider no longer used close
                        # by; unknown when the team could not be read.
                        **(
                            {
                                "providers": [
                                    provider.name
                                    for provider in configured_providers(context.team)
                                ]
                            }
                            if context is not None
                            else {}
                        ),
                    },
                )
                return response
            finally:
                if context is not None:
                    await context.aclose()

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
                    "directories": directories,
                },
                ensure_ascii=False,
            )

        trace_id, outcome = await self._execute_command(
            _Execution(
                command="assistants/troubleshoot",
                label=f"troubleshoot:{focus.trace_id or focus.view}",
                cwd=partial(_assistant_cwd, "troubleshooting"),
                failure_code="troubleshooting_failed",
                failure_status=502,
                attributes={"troubleshooting.conversation_id": request.conversation_id},
                result_type=TroubleshootingResult,
                work=Work("troubleshooting", request.conversation_id),
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
                if candidate
                and self._diagnostics_store.get_summary(candidate) is not None
            ],
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
            identity_records=ActivityEventStore.of(get_identity_root()).records_between(
                start_time, end_time
            ),
            detail_available=self._diagnostics_store.transcript_exists,
        )

    def _refresh_activity_events(
        self, team: Team, start: datetime, end: datetime, *, force: bool
    ) -> None:
        """Refresh the shared activity of the services at most once per five minutes."""
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
            # five-second history refresh into a five-second service retry loop.
            self._activity_sync_attempts[period] = now
            threading.Thread(
                target=self._sync_activity_events,
                args=(team, start, end, period),
                name="guildbotics-activity-sync",
                daemon=True,
            ).start()

    def _sync_activity_events(
        self, team: Team, start: datetime, end: datetime, period: tuple[str, str]
    ) -> None:
        try:
            asyncio.run(refresh_activity_events(team, start, end))
            if end <= datetime.now(UTC):
                _mark_activity_week_completed(period)
        except Exception as exc:
            self.event_bus.publish_log("WARNING", f"Activity refresh failed: {exc}")

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
        records.extend(
            item
            for item in self._diagnostics_store.records_between(includes=includes)
            if item.get("type") == TARGET_RECORDED_EVENT_TYPE
        )
        records.extend(
            MemoryAuditStore().list_events(
                since=start.isoformat(), until=end.isoformat()
            )
        )
        return records

    async def get_cli_agent_usage(
        self, agent_name: str, refresh: bool = False
    ) -> CliAgentUsageResponse:
        """Return one AI CLI tool's account usage (see :class:`CliAgentUsageCache`)."""
        return await self._cli_agent_usage.read(agent_name, refresh)

    async def close_cli_agent_usage(self) -> None:
        """Cancel running usage probes so their environments are released."""
        await self._cli_agent_usage.aclose()

    def saved_team(self) -> Team:
        """The workspace's saved team, with its members' secrets in reach."""
        self._load_workspace_env()
        return YamlTeamLoader().load()

    def _get_context(self) -> Context:
        self._load_workspace_env()
        try:
            return create_context()
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


def _timestamp_in_range(value: str, start: datetime, end: datetime) -> bool:
    parsed = parse_timestamp(value)
    return parsed is not None and start <= parsed < end


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


def _assistant_cwd(name: str) -> Path:
    """Where a Desktop assistant's turns work: its own directory under
    ``.guildbotics/local/work``."""
    cwd = get_workspace_work_path(name, workspace_root=get_workspace_root())
    from guildbotics.environment.contract import (
        protected_paths,
        validate_mount_source,
    )

    return validate_mount_source(cwd, protected_paths(), create=True)
