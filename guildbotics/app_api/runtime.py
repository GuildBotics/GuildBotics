from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shlex
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from guildbotics.app_api.activity_history import build_activity_history, parse_timestamp
from guildbotics.app_api.agent_streams import collapse_assistant_streams
from guildbotics.app_api.command_files import CommandFileService, file_revision
from guildbotics.app_api.diagnostics import ScenarioDiagnosticsService
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.intelligences import CLI_BRAIN_CLASS
from guildbotics.app_api.lifecycle import RuntimeLifecycleService
from guildbotics.app_api.models import (
    ActivityHistoryResponse,
    AgentFieldOption,
    AgentFieldStateResponse,
    ChatReceiveResetResponse,
    CliAgentDetection,
    CliAgentDetectionsResponse,
    CliAgentUsage,
    CliAgentUsagesResponse,
    CliAgentUsageWindow,
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
from guildbotics.app_api.trace_presentations import normalize_trace_presentation
from guildbotics.app_api.verify import VerifyService
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.capabilities.github_activity_events import (
    refresh_github_activity_events,
)
from guildbotics.capabilities.member_memory_audit import (
    DEFAULT_MEMORY_AUDIT_MAX_BYTES,
    MemoryAuditStore,
    parse_memory_audit_timestamp,
)
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.authoring import author_command_turn
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
    default_command_label,
    load_command_metadata,
    parse_command_arguments,
    parse_command_input_policy,
)
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_command_source,
)
from guildbotics.drivers import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.drivers.execution import (
    ExecutionCoordinator,
    WorkRejectedError,
    WorkSource,
)
from guildbotics.editions import get_edition
from guildbotics.entities import Person, Project, Service, Team
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.intelligences.agent_runtime.usage import (
    CLI_AGENT_USAGE_READERS,
    CliAgentUsageError,
    CliAgentUsageSnapshot,
)
from guildbotics.intelligences.brains.cli_agent import CliAgentExecutionError
from guildbotics.intelligences.cli_agents import CLI_AGENTS, resolve_cli_agent_path
from guildbotics.intelligences.troubleshooting import troubleshoot_turn
from guildbotics.observability import new_id, trace_scope
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import (
    DEFAULT_DIAGNOSTICS_MAX_BYTES,
    DiagnosticsStore,
)
from guildbotics.observability.session_transcripts import (
    transcript_detail,
    transcript_retention_days,
)
from guildbotics.runtime import Context
from guildbotics.runtime.local_command_executor import LocalCommandExecutor
from guildbotics.runtime.member_context import resolve_person
from guildbotics.runtime.service_lock import ServiceLockUnavailableError
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
    load_yaml_file,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.workspace_state import write_active_workspace

WORKSPACE_DOTENV_PROTECTED_KEYS = {
    GUILDBOTICS_WORKSPACE_ROOT,
    *HOME_ENV_PROTECTED_KEYS,
}
MIN_MEMORY_DOCUMENT_PATH_PARTS = 2
# Desktop AI assistant turns are manual runs: user-initiated, frequent, and
# scoped to one session. The agent work kind stays separate from this.
ASSISTANT_TRACE_SOURCE: WorkSource = "manual"
ACTIVITY_SYNC_COOLDOWN_SECONDS = 5 * 60
ACTIVITY_SYNC_STATE_FILE = "activity_sync_weeks.json"
ACTIVITY_SYNC_PERIOD_PARTS = 2


class _UseProcessDataDir:
    pass


_USE_PROCESS_DATA_DIR = _UseProcessDataDir()

_CLI_AGENT_USAGE_TTL_SECONDS = 300.0


def _cli_agent_usage_model(snapshot: CliAgentUsageSnapshot) -> CliAgentUsage:
    return CliAgentUsage(
        agent=snapshot.agent,
        windows=[
            CliAgentUsageWindow(
                window=window.window,
                used_percent=window.used_percent,
                resets_at=window.resets_at,
                window_minutes=window.window_minutes,
                label=window.label,
                detail=window.detail,
            )
            for window in snapshot.windows
        ],
        limit_reached=snapshot.limit_reached,
        checked_at=snapshot.checked_at,
    )


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
        self._execution = ExecutionCoordinator()
        self._cli_agent_usage_lock = asyncio.Lock()
        self._cli_agent_usage_cache: tuple[float, CliAgentUsagesResponse] | None = None
        self._loaded_dotenv_keys: set[str] = set()
        self._workspace_sync = WorkspaceSyncService()
        if load_workspace_environment:
            self._load_workspace_env()
        self._lifecycle = RuntimeLifecycleService(
            event_bus=event_bus,
            context_factory=self._get_context,
            stop_timeout_seconds=stop_timeout_seconds,
            execution_coordinator=self._execution,
        )

    @property
    def system_service_run_id(self) -> str:
        return self._system_service_run_id

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
            cwd=Path.cwd(),
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
        workspace = workspace_dir.expanduser().resolve()
        if not workspace.exists():
            raise AppApiError(
                "workspace_not_found",
                "Workspace directory was not found.",
                context={"workspace_dir": str(workspace)},
                status_code=400,
            )
        if not workspace.is_dir():
            raise AppApiError(
                "workspace_not_directory",
                "Workspace path must be a directory.",
                context={"workspace_dir": str(workspace)},
                status_code=400,
            )
        # Reject up front so a running service is not force-stopped just
        # because a switch was requested.
        status = self.get_scheduler_status()
        if _runtime_has_active_work(status):
            raise _workspace_switch_blocked_error(status)
        # Force-stop anything that slipped in between the check above and here.
        # A forced stop can still time out (uncancellable work, drain timeout),
        # so re-check and abort rather than switching cwd/env under live work.
        stopped = self.stop_scheduler(force=True)
        if _runtime_has_active_work(stopped):
            raise _workspace_switch_blocked_error(stopped)
        if self._diagnostics_store is not None:
            self._diagnostics_store.finish_system_session()
        # Stopped before the switch so the queue of the workspace being left
        # cannot commit into the one being entered.
        self._workspace_sync.deactivate()
        os.chdir(workspace)
        write_active_workspace(workspace)
        apply_workspace_root(workspace)
        self._load_workspace_env()
        if self._diagnostics_store is not None:
            self._diagnostics_store.start_system_session(self._system_service_run_id)
            self._diagnostics_store.start_maintenance()
        self._workspace_sync.activate()
        return self.get_config_status()

    def get_team_summary(self) -> TeamSummary:
        context = self._get_context()
        project = context.team.project
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
                    person_type=getattr(member, "person_type", ""),
                    is_active=member.is_active,
                    roles=sorted(member.roles.keys()),
                )
                for member in context.team.members
            ],
            default_person_id=context.team.get_default_person_id(),
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

    def list_command_files(self) -> CommandFilesResponse:
        return self._command_file_service().list_files()

    def get_command_file(self, file_id: str) -> CommandFileDetail:
        return self._command_file_service().read_file(file_id)

    def create_command_file(
        self, request: CommandFileCreateRequest
    ) -> CommandFileDetail:
        return self._command_file_service().create_file(
            request.command, request.format, request.content
        )

    def update_command_file(
        self, file_id: str, request: CommandFileUpdateRequest
    ) -> CommandFileDetail:
        return self._command_file_service().update_file(
            file_id, request.content, request.expected_revision
        )

    def delete_command_file(
        self, file_id: str, expected_revision: str
    ) -> CommandFilesResponse:
        return self._command_file_service().delete_file(file_id, expected_revision)

    @asynccontextmanager
    async def _assistant_turn(
        self,
        *,
        work_kind: str,
        label: str,
        conversation_id: str,
        message: str,
        person: str | None,
        failure_code: str,
        read_only: bool = False,
    ) -> AsyncIterator[tuple[Context, str]]:
        """Scope one Desktop AI assistant turn.

        Resolves the acting member, tracks the turn as cancellable manual work
        and correlates everything it records under a fresh trace.

        Args:
            work_kind: Trace name and agent work kind, such as ``troubleshooting``.
            label: Command label reported in the runtime status and trace.
            conversation_id: Stable identity shared by every turn of the conversation.
            message: Latest user instruction, used to resolve the context language.
            person: Requested member identifier, or ``None`` for the team default.
            failure_code: App API error code used when the assistant fails.
            read_only: Whether the turn is enforced read-only by the agent
                adapter. Only such a turn skips the member's execution lease, so
                it stays usable while that member runs scheduled work. A turn
                that can write keeps the lease it has always held.

        Yields:
            The member-scoped context and this turn's trace id.

        Raises:
            AppApiError: If the member cannot be resolved, the runtime rejects
                the work, or the assistant fails.
        """
        base_context = self._get_context(message)
        acting = self._resolve_execution_person(base_context, person, label)
        context = base_context.clone_for(acting)
        trace_id = new_id()
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def _cancel_turn() -> None:
            if task is not None:
                loop.call_soon_threadsafe(task.cancel)

        try:
            with (
                self._execution.track_work(
                    source=ASSISTANT_TRACE_SOURCE,
                    person_id=acting.person_id,
                    command=label,
                    work_id=trace_id,
                    cancel=_cancel_turn,
                    exclusive=not read_only,
                ),
                trace_scope(
                    # An assistant turn is a Desktop-initiated run like any
                    # other manual command, so it belongs to that source: it is
                    # then filterable in diagnostics and, like other manual
                    # runs, stays off the activity timeline it fires too often
                    # for. `work_kind` scopes the provider conversation, not the
                    # trace, and the `label` prefix still identifies the turn.
                    ASSISTANT_TRACE_SOURCE,
                    command=label,
                    person_id=acting.person_id,
                    trace_id=trace_id,
                    attributes={f"{work_kind}.conversation_id": conversation_id},
                ),
            ):
                yield context, trace_id
        except AppApiError:
            raise
        except WorkRejectedError as exc:
            raise AppApiError("work_rejected", str(exc), status_code=409) from exc
        except (CommandError, CliAgentExecutionError) as exc:
            # The turn drives a foreign agent: an AI CLI tool that exits
            # non-zero, a provider that rejects the credential, a response the
            # prompt cannot use. Every one of them is a failed assistant turn
            # the panel must be able to explain with the tool's own reason.
            # Anything else is a defect, not an agent failure: it stays a
            # generic 500 so internal wording never reaches the client.
            raise AppApiError(failure_code, str(exc), status_code=502) from exc
        finally:
            await context.aclose()

    async def author_command(
        self, request: CommandAuthoringRequest
    ) -> CommandAuthoringResponse:
        """Return one answer or reviewed-change proposal for shared commands."""
        command_label = request.command or "new-command"
        async with self._assistant_turn(
            work_kind="command_authoring",
            label=f"author:{command_label}",
            conversation_id=request.conversation_id,
            message=request.message,
            person=request.person,
            failure_code="command_authoring_failed",
            read_only=True,
        ) as (context, trace_id):
            result = await author_command_turn(
                context,
                mode=request.mode,
                conversation_id=request.conversation_id,
                trace_id=trace_id,
                command=request.command,
                command_format=request.format,
                content=request.content,
                instruction=request.message,
                available_commands=self._command_authoring_context(),
                workspace_data_root=get_workspace_root(),
            )
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
            files=self._command_file_service().apply_authoring_changes(request.changes)
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
                f"Person '{person}' not found.",
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
                "The selected command file is not the runtime execution target.",
                status_code=409,
                context=blocking_context,
            )

    async def run_command(self, request: CommandRunRequest) -> CommandRunResponse:
        context = self._get_context(request.message)
        # Resolve the member up front: the guard must check the file that this
        # very member runs, and an omitted person would otherwise resolve twice
        # (placeholder context for the guard, team default for the run).
        person = self._resolve_execution_person(
            context, request.person, request.command
        )
        person_id = person.person_id
        if request.expected_command_file_id is not None:
            self._guard_run_target(request, context.clone_for(person))
        trace_id = new_id()
        self._reserve_command(trace_id)
        try:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()

            def _cancel_manual_command() -> None:
                if task is not None:
                    loop.call_soon_threadsafe(task.cancel)

            try:
                with (
                    self._execution.track_work(
                        source="manual",
                        person_id=person_id,
                        command=request.command,
                        work_id=trace_id,
                        cancel=_cancel_manual_command,
                    ),
                    trace_scope(
                        "manual",
                        command=request.command,
                        person_id=person_id,
                        trace_id=trace_id,
                    ),
                ):
                    output = await self._run_command_traced(request, context, person_id)
            except WorkRejectedError as exc:
                raise AppApiError(
                    "work_rejected",
                    str(exc),
                    status_code=409,
                ) from exc
        finally:
            self._release_command(trace_id)
        return CommandRunResponse(trace_id=trace_id, output=output)

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
                "Specify a person using person or '<command>@person'."
                f" Available: {', '.join(available) if available else 'none'}",
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
                f"Person '{exc.identifier}' not found."
                f" Available: {', '.join(available) if available else 'none'}",
                context={"identifier": exc.identifier, "available": available},
            ) from exc

    async def _run_command_traced(
        self, request: CommandRunRequest, context: Context, person_id: str
    ) -> str:
        # Events carry the resolved person so an omitted request person still
        # shows the member the run actually belongs to.
        self._event_bus.publish_event(
            "command.started",
            {"command": request.command, "person": person_id},
        )
        try:
            output = await LocalCommandExecutor().run(
                context,
                command_name=request.command,
                command_args=request.args,
                person_identifier=person_id,
                cwd=request.cwd,
            )
        except asyncio.CancelledError:
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": request.command,
                    "person": person_id,
                    "code": "cancelled",
                    "message": "Command was cancelled.",
                },
            )
            raise
        except CommandError as exc:
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": request.command,
                    "code": "command_error",
                    "message": str(exc),
                },
            )
            raise AppApiError("command_error", str(exc)) from exc
        except Exception as exc:
            self._event_bus.publish_event(
                "command.failed",
                {"command": request.command, "error_type": type(exc).__name__},
            )
            raise
        self._event_bus.publish_event(
            "command.finished",
            {"command": request.command, "output_length": len(output)},
        )
        return output

    def start_scheduler(self, request: SchedulerStartRequest) -> RuntimeStatus:
        try:
            return self._lifecycle.start(request)
        except ServiceLockUnavailableError as exc:
            metadata = exc.metadata
            raise AppApiError(
                "service_already_running",
                t("runtime.service_lock.already_running"),
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

    def stop_scheduler(self, *, force: bool = False) -> RuntimeStatus:
        return self._lifecycle.stop(force=force)

    def get_scheduler_status(self) -> RuntimeStatus:
        return self._lifecycle.get_status()

    def get_system_alerts(self) -> SystemAlertsResponse:
        return self._system_alerts.list_alerts(self.get_scheduler_status())

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
                "Stop the service before resetting chat receive state.",
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
        memory_path = MemoryAuditStore().path
        try:
            memory_size = memory_path.stat().st_size
        except OSError:
            memory_size = 0
        return TranscriptSettingsStatus(
            detail=cast(Any, transcript_detail()),
            retention_days=transcript_retention_days(),
            sessions_dir=get_workspace_local_path("run", "sessions"),
            total_size_bytes=int(usage["total_size_bytes"]),
            index_size_bytes=int(usage["index_size_bytes"]),
            index_rewrite_threshold_bytes=DEFAULT_DIAGNOSTICS_MAX_BYTES,
            memory_size_bytes=memory_size,
            memory_max_size_bytes=DEFAULT_MEMORY_AUDIT_MAX_BYTES,
        )

    def update_transcript_settings(
        self, request: TranscriptSettingsUpdateRequest
    ) -> TranscriptSettingsStatus:
        from guildbotics.observability.session_transcripts import (
            write_transcript_settings,
        )

        write_transcript_settings(
            detail=request.detail, retention_days=request.retention_days
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
        )
        traces = [TraceSummary.model_validate(summary) for summary in summaries]
        return TracesResponse(traces=traces)

    def get_trace_detail(self, trace_id: str) -> TraceDetailResponse:
        records: list[TraceRecord] = []
        summary = None
        if self._diagnostics_store is not None:
            raw_summary = self._diagnostics_store.get_summary(trace_id)
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
        label = f"troubleshoot:{focus.trace_id or focus.view}"
        async with self._assistant_turn(
            work_kind="troubleshooting",
            label=label,
            conversation_id=request.conversation_id,
            message=request.message,
            person=request.person,
            failure_code="troubleshooting_failed",
            read_only=True,
        ) as (context, trace_id):
            result = await troubleshoot_turn(
                context,
                conversation_id=request.conversation_id,
                trace_id=trace_id,
                question=request.message,
                focus=focus.model_dump(),
                workspace_data_root=get_workspace_root(),
            )
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
        limit: int = 1000,
        refresh: bool = False,
        sync_start: str | None = None,
        sync_end: str | None = None,
    ) -> ActivityHistoryResponse:
        end_time = parse_timestamp(end or "") or datetime.now(UTC)
        start_time = parse_timestamp(start or "") or (end_time - timedelta(days=7))
        if start_time > end_time:
            raise AppApiError(
                "invalid_activity_history_range",
                "Activity history start must be before end.",
                context={"start": start or "", "end": end or ""},
                status_code=400,
            )
        if (sync_start is None) != (sync_end is None):
            raise AppApiError(
                "invalid_activity_sync_range",
                "Activity sync start and end must be provided together.",
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
                    "Activity sync start and end must be valid timestamps.",
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
                    "Activity sync start must be before end.",
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
        records = self._activity_records_between(start_time, end_time, limit=limit)
        run_store = RunStore()
        run_summaries = run_store.summaries_by_subject()
        run_subjects = run_store.subjects_by_run()
        return build_activity_history(
            start=start_time,
            end=end_time,
            members=context.team.members,
            records=records,
            run_summary=lambda subject_id, person_id: run_summaries.get(
                (subject_id, person_id), ""
            ),
            run_subject=lambda run_id: run_subjects.get(run_id, ""),
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
        self, start: datetime, end: datetime, *, limit: int
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def includes(value: str) -> bool:
            return _timestamp_in_range(value, start, end)

        diagnostics_records: list[dict[str, Any]] = (
            self._diagnostics_store.records_between(includes=includes, limit=limit)
            if self._diagnostics_store is not None
            else []
        )
        # Shared activity events may originate on another device; only traces
        # this device's diagnostics actually know get a detail link.
        local_trace_ids = {
            trace_id
            for item in diagnostics_records
            if (trace_id := item.get("trace_id"))
        }
        for item in ActivityEventStore().records_between(start, end, limit=limit):
            if item.get("trace_id") not in local_trace_ids:
                item["trace_id"] = None
            records.append(item)
        records.extend(
            item
            for item in diagnostics_records
            if item.get("type")
            not in {"session.pointer", "system.started", "system.finished"}
        )
        records.extend(
            MemoryAuditStore().list_events(
                since=start.isoformat(),
                until=end.isoformat(),
                limit=limit,
            )
        )
        records.sort(key=_activity_record_sort_key)
        return records[-max(1, limit) :]

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

    def detect_cli_agents(self) -> CliAgentDetectionsResponse:
        agents: list[CliAgentDetection] = []
        for info in CLI_AGENTS:
            path = resolve_cli_agent_path(info.executable)
            agents.append(
                CliAgentDetection(
                    name=info.name,
                    label=info.label,
                    executable=info.executable,
                    config_reference=info.config_reference,
                    detected=bool(path),
                    path=path,
                )
            )
        return CliAgentDetectionsResponse(agents=agents)

    async def get_cli_agent_usage(
        self, refresh: bool = False
    ) -> CliAgentUsagesResponse:
        """Return account usage per AI CLI tool, cached for a short TTL.

        Only tools with a structured usage interface (the readers in
        ``CLI_AGENT_USAGE_READERS``) appear in the response; the frontend
        shows nothing for the rest.
        """
        async with self._cli_agent_usage_lock:
            now = time.monotonic()
            cached = self._cli_agent_usage_cache
            if (
                not refresh
                and cached is not None
                and now - cached[0] < _CLI_AGENT_USAGE_TTL_SECONDS
            ):
                return cached[1]
            usages: list[CliAgentUsage] = []
            for agent in self.detect_cli_agents().agents:
                reader = CLI_AGENT_USAGE_READERS.get(agent.name)
                if reader is None or not agent.detected:
                    continue
                try:
                    snapshot = await reader(agent.path or agent.executable)
                except CliAgentUsageError as exc:
                    logging.getLogger("guildbotics.app_api.cli_agent_usage").warning(
                        "Could not read %s usage: %s", agent.name, exc
                    )
                    continue
                usages.append(_cli_agent_usage_model(snapshot))
            response = CliAgentUsagesResponse(usages=usages)
            self._cli_agent_usage_cache = (now, response)
            return response

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
                "GuildBotics configuration is not available. Run config init first.",
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

    def _reserve_command(self, trace_id: str) -> None:
        with self._lock:
            if self._running_command_id is not None:
                raise AppApiError(
                    "command_already_running",
                    "Another command is already running.",
                    status_code=409,
                    context={"trace_id": self._running_command_id},
                )
            self._running_command_id = trace_id

    def _release_command(self, trace_id: str) -> None:
        with self._lock:
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


def _activity_record_sort_key(item: dict[str, Any]) -> datetime:
    parsed = parse_timestamp(str(item.get("timestamp") or ""))
    return (
        parsed.astimezone(UTC)
        if parsed is not None
        else datetime.min.replace(tzinfo=UTC)
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


def _runtime_has_active_work(status: RuntimeStatus) -> bool:
    return (
        status.scheduler.running
        or status.events.running
        or bool(status.active_works)
        or status.scheduler.state == "stopping"
        or status.events.state == "stopping"
    )


def _workspace_switch_blocked_error(status: RuntimeStatus) -> AppApiError:
    return AppApiError(
        "workspace_switch_blocked_by_active_work",
        "Service or command work is still running. Stop it before switching workspaces.",
        context={
            "active_work_count": len(status.active_works),
            "scheduler_state": status.scheduler.state,
            "events_state": status.events.state,
        },
        status_code=409,
    )
