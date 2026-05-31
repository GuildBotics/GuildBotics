from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from guildbotics.app_api.diagnostics import ScenarioDiagnosticsService
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import CommandEventLogHandler, EventBus
from guildbotics.app_api.lifecycle import RuntimeLifecycleService
from guildbotics.app_api.models import (
    CliAgentDetection,
    CliAgentDetectionsResponse,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    MemberSummary,
    ProjectSummary,
    RuntimeStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    TeamSummary,
    VerifyResponse,
)
from guildbotics.app_api.verify import VerifyService
from guildbotics.cli import get_setup_tool
from guildbotics.drivers import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    run_command,
)
from guildbotics.entities import Service
from guildbotics.runtime import Context
from guildbotics.utils.fileio import (
    CONFIG_PATH,
    get_storage_path,
    get_template_path,
    load_yaml_file,
)


class AppRuntime:
    def __init__(
        self, event_bus: EventBus, *, stop_timeout_seconds: float = 10.0
    ) -> None:
        self._event_bus = event_bus
        self._lock = threading.Lock()
        self._running_command_id: str | None = None
        self._lifecycle = RuntimeLifecycleService(
            event_bus=event_bus,
            context_factory=self._get_context,
            default_routines_factory=lambda: get_setup_tool().get_default_routines(),
            stop_timeout_seconds=stop_timeout_seconds,
        )

    def get_config_status(self) -> ConfigStatus:
        primary_config_dir = Path(os.getenv("GUILDBOTICS_CONFIG_DIR", CONFIG_PATH))
        primary_project_file = primary_config_dir / "team" / "project.yml"
        home_config_dir = Path.home() / CONFIG_PATH
        home_project_file = home_config_dir / "team" / "project.yml"
        env_file = Path.cwd() / ".env"
        return ConfigStatus(
            cwd=Path.cwd(),
            env_file=env_file,
            env_file_exists=env_file.exists(),
            primary_config_dir=primary_config_dir,
            primary_project_file=primary_project_file,
            primary_project_file_exists=primary_project_file.exists(),
            home_config_dir=home_config_dir,
            home_project_file=home_project_file,
            home_project_file_exists=home_project_file.exists(),
            storage_dir=get_storage_path(),
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
        self.stop_scheduler()
        os.chdir(workspace)
        self._load_workspace_env()
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
                    is_active=member.is_active,
                    roles=sorted(member.roles.keys()),
                )
                for member in context.team.members
            ],
        )

    async def run_command(self, request: CommandRunRequest) -> CommandRunResponse:
        request_id = uuid.uuid4().hex
        self._reserve_command(request_id)
        self._event_bus.publish_event(
            "command.started",
            {"command": request.command, "person": request.person},
            request_id=request_id,
        )
        logger = logging.getLogger("guildbotics")
        log_handler = CommandEventLogHandler(self._event_bus, request_id)
        log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(log_handler)
        try:
            context = self._get_context(request.message)
            output = await run_command(
                context,
                command_name=request.command,
                command_args=request.args,
                person_identifier=request.person,
                cwd=request.cwd,
            )
        except PersonSelectionRequiredError as exc:
            available = list(exc.available)
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": request.command,
                    "code": "person_selection_required",
                    "available": available,
                },
                request_id=request_id,
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
                    "command": request.command,
                    "code": "person_not_found",
                    "identifier": exc.identifier,
                    "available": available,
                },
                request_id=request_id,
            )
            raise AppApiError(
                "person_not_found",
                f"Person '{exc.identifier}' not found."
                f" Available: {', '.join(available) if available else 'none'}",
                context={"identifier": exc.identifier, "available": available},
            ) from exc
        except CommandError as exc:
            self._event_bus.publish_event(
                "command.failed",
                {
                    "command": request.command,
                    "code": "command_error",
                    "message": str(exc),
                },
                request_id=request_id,
            )
            raise AppApiError("command_error", str(exc)) from exc
        except Exception as exc:
            self._event_bus.publish_event(
                "command.failed",
                {"command": request.command, "error_type": type(exc).__name__},
                request_id=request_id,
            )
            raise
        finally:
            logger.removeHandler(log_handler)
            self._release_command(request_id)
        self._event_bus.publish_event(
            "command.finished",
            {"command": request.command, "output_length": len(output)},
            request_id=request_id,
        )
        return CommandRunResponse(request_id=request_id, output=output)

    def start_scheduler(self, request: SchedulerStartRequest) -> RuntimeStatus:
        if request.only != "events":
            routine_commands = (
                request.routine_commands or get_setup_tool().get_default_routines()
            )
            if not self.is_github_integration_enabled():
                for command in routine_commands:
                    if self.requires_github_for_routine(command):
                        raise AppApiError(
                            "github_integration_required_for_routine",
                            "GitHub integration is required for the selected routine command.",
                            context={"command": command},
                            status_code=400,
                        )
        return self._lifecycle.start(request)

    def stop_scheduler(self) -> RuntimeStatus:
        return self._lifecycle.stop()

    def get_scheduler_status(self) -> RuntimeStatus:
        return self._lifecycle.get_status()

    def verify(self) -> VerifyResponse:
        status = self.get_config_status()
        team = None
        team_error = None
        try:
            team = self._get_context().team
        except Exception as exc:
            team_error = exc

        return VerifyService().verify(config=status, team=team, team_error=team_error)

    async def run_scenario_diagnostics(
        self, person_id: str | None = None
    ) -> ScenarioDiagnosticsResponse:
        context = None
        context_error = None
        try:
            context = self._get_context()
        except Exception as exc:
            context_error = exc
        try:
            return await ScenarioDiagnosticsService().run(
                context=context,
                context_error=context_error,
                person_id=person_id,
            )
        finally:
            if context is not None:
                await context.aclose()

    def detect_cli_agents(self) -> CliAgentDetectionsResponse:
        mapping: dict[str, Any] = {}
        try:
            mapping_file = get_template_path() / "intelligences/cli_agent_mapping.yml"
            mapping = cast(dict[str, Any], load_yaml_file(mapping_file))
        except Exception:
            mapping = {}
        agents: list[CliAgentDetection] = []
        for name in ("codex", "gemini", "claude", "copilot"):
            executable_info_file = str(mapping.get(name, ""))
            script = self._load_cli_agent_script(executable_info_file)
            executable = self._resolve_cli_executable(script)
            path = (
                shutil.which(executable, path=os.environ.get("PATH"))
                if executable
                else None
            )
            agents.append(
                CliAgentDetection(
                    name=name,
                    executable=executable,
                    detected=path is not None,
                    path=path or "",
                )
            )
        return CliAgentDetectionsResponse(agents=agents)

    def get_default_routines(self) -> list[str]:
        return list(get_setup_tool().get_default_routines())

    def is_github_integration_enabled(self) -> bool:
        try:
            return self._get_context().team.project.is_available_service(
                Service.TICKET_MANAGER
            )
        except Exception:
            return False

    def requires_github_for_routine(self, command: str) -> bool:
        return command == "workflows/ticket_driven_workflow"

    def _load_cli_agent_script(self, executable_info_file: str) -> str:
        if not executable_info_file:
            return ""
        try:
            executable_info = cast(
                dict[str, Any],
                load_yaml_file(
                    get_template_path()
                    / f"intelligences/cli_agents/{executable_info_file}"
                ),
            )
            return str(executable_info.get("script", ""))
        except Exception:
            return ""

    def _resolve_cli_executable(self, script: str) -> str:
        for executable in ("codex", "gemini", "claude", "copilot"):
            if executable in script:
                return executable
        return ""

    def _get_context(self, message: str = "") -> Context:
        self._load_workspace_env()
        try:
            return get_setup_tool().get_context(message)
        except FileNotFoundError as exc:
            raise AppApiError(
                "config_not_found",
                "GuildBotics configuration is not available. Run config init first.",
                context={"path": str(exc.filename or "")},
            ) from exc

    def _load_workspace_env(self) -> None:
        dotenv_path = Path.cwd() / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path, override=True)

    def _reserve_command(self, request_id: str) -> None:
        with self._lock:
            if self._running_command_id is not None:
                raise AppApiError(
                    "command_already_running",
                    "Another command is already running.",
                    status_code=409,
                    context={"request_id": self._running_command_id},
                )
            self._running_command_id = request_id

    def _release_command(self, request_id: str) -> None:
        with self._lock:
            if self._running_command_id == request_id:
                self._running_command_id = None
