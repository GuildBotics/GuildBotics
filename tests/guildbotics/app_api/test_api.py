import asyncio
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from yaml import safe_load

from guildbotics.app_api.api import create_app
from guildbotics.app_api.diagnostics_store import DiagnosticsStore
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    AgentFieldOption,
    AgentFieldStateResponse,
    CliAgentDetectionsResponse,
    CommandOption,
    CommandOptionsResponse,
    CommandRequirement,
    CommandRunRequest,
    ConfigStatus,
    DiagnosticCheck,
    ProjectStatusOptionsRequest,
    ProjectStatusOptionsResponse,
    PromptTraceStatus,
    PromptTraceUpdateRequest,
    RoutineCommandOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    RuntimeUnitStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    TeamSummary,
    VerifyCheck,
    VerifyResponse,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.editions.simple.setup_service import (
    GitHubUserReference,
    SetupServiceError,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability import trace_scope

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_CONFLICT = 409
THREAD_WAIT_SECONDS = 2.0
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_ROUTINE_INTERVAL_MINUTES = 10
GITHUB_APPS_USER_ID = 42

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}


def _client(runtime: "RuntimeStub") -> TestClient:
    """Build a TestClient bound to a stubbed runtime with a fixed token."""
    return TestClient(create_app(session_token="secret", runtime=runtime))


def _runtime_status(
    *,
    scheduler_state: str = "stopped",
    events_state: str = "stopped",
) -> RuntimeStatus:
    return RuntimeStatus(
        scheduler=RuntimeUnitStatus(
            target="scheduler",
            state=scheduler_state,
            running=scheduler_state in {"starting", "running", "stopping"},
        ),
        events=RuntimeUnitStatus(
            target="events",
            state=events_state,
            running=events_state in {"starting", "running", "stopping"},
        ),
    )


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            env_file=tmp_path / ".env",
            env_file_exists=False,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=False,
            storage_dir=tmp_path / "home/.guildbotics/data",
        )

    def stop_scheduler(self) -> RuntimeStatus:
        return _runtime_status()

    def get_config_status(self) -> ConfigStatus:
        return self.config_status

    async def fetch_project_status_options(
        self, request: ProjectStatusOptionsRequest
    ) -> ProjectStatusOptionsResponse:
        self.status_options_request = request
        return getattr(
            self, "status_options", ProjectStatusOptionsResponse(available=False)
        )

    async def fetch_agent_field_state(
        self, request: ProjectStatusOptionsRequest
    ) -> AgentFieldStateResponse:
        self.agent_field_request = request
        return getattr(
            self, "agent_field_state", AgentFieldStateResponse(available=False)
        )

    async def ensure_agent_field(
        self, request: ProjectStatusOptionsRequest
    ) -> AgentFieldStateResponse:
        self.agent_field_ensure_request = request
        return getattr(
            self, "agent_field_state", AgentFieldStateResponse(available=False)
        )

    def set_workspace(self, workspace_dir: Path) -> ConfigStatus:
        project_file = workspace_dir / ".guildbotics/config/team/project.yml"
        config_dir = workspace_dir / ".guildbotics/config"
        project_file_exists = project_file.exists()
        self.config_status = self.config_status.model_copy(
            update={
                "cwd": workspace_dir,
                "env_file": workspace_dir / ".env",
                "env_file_exists": (workspace_dir / ".env").exists(),
                "config_dir": config_dir,
                "project_file": project_file,
                "project_file_exists": project_file_exists,
            }
        )
        return self.config_status

    def get_team_summary(self) -> TeamSummary:
        return TeamSummary(
            project={
                "name": "GuildBotics",
                "language_code": "en",
                "language_name": "English",
            },
            members=[],
        )

    async def run_command(self, request):
        if request.command == "missing":
            raise AppApiError(
                "command_error",
                "Unable to locate command 'missing'.",
                context={"command": request.command},
            )
        return {"trace_id": "stub-request", "output": f"ran {request.command}"}

    def get_command_options(self, person: str | None = None) -> CommandOptionsResponse:
        return CommandOptionsResponse(
            options=[
                CommandOption(
                    command="hello",
                    label="Hello",
                    category="custom",
                    source="workspace",
                    path=self.config_status.cwd
                    / ".guildbotics/config/commands/hello.py",
                )
            ]
        )

    def get_routine_command_options(
        self, person: str | None = None
    ) -> RoutineCommandOptionsResponse:
        options = list(self.get_command_options(person).options)
        options.append(
            CommandOption(
                command="workflows/ticket_driven_workflow",
                label="Ticket Driven Workflow",
                category="workflow",
                source="template",
                path=self.config_status.cwd
                / "templates/commands/workflows/ticket_driven_workflow.py",
            )
        )
        return RoutineCommandOptionsResponse(
            options=options, default_command="workflows/ticket_driven_workflow"
        )

    def get_scheduler_status(self) -> RuntimeStatus:
        return _runtime_status()

    def start_scheduler(self, request) -> RuntimeStatus:
        scheduler_running = (
            request.sources.scheduled
            or request.sources.routine
            or request.sources.event_queue
        )
        return _runtime_status(
            scheduler_state="running" if scheduler_running else "stopped",
            events_state="running" if request.sources.event_queue else "stopped",
        )

    def get_prompt_trace_status(
        self, limit: int = 20, read_path: str | None = None
    ) -> PromptTraceStatus:
        output_trace_file = self.config_status.storage_dir / "run/prompt_trace.jsonl"
        trace_file = Path(read_path) if read_path else output_trace_file
        return PromptTraceStatus(
            enabled=False,
            env_file=self.config_status.env_file,
            env_file_exists=self.config_status.env_file.exists(),
            trace_file=trace_file,
            output_trace_file=output_trace_file,
            default_trace_file=output_trace_file,
            trace_file_exists=False,
            event_count=0,
            events=[],
        )

    def update_prompt_trace(
        self, request: PromptTraceUpdateRequest, *, limit: int = 20
    ) -> PromptTraceStatus:
        output_trace_file = (
            Path(request.trace_path)
            if request.trace_path
            else self.config_status.storage_dir / "run/prompt_trace.jsonl"
        )
        return PromptTraceStatus(
            enabled=request.enabled,
            env_file=self.config_status.env_file,
            env_file_exists=True,
            trace_file=output_trace_file,
            output_trace_file=output_trace_file,
            default_trace_file=self.config_status.storage_dir
            / "run/prompt_trace.jsonl",
            trace_file_exists=False,
            event_count=0,
            events=[],
        )

    def get_runtime_debug_status(self) -> RuntimeDebugStatus:
        return RuntimeDebugStatus(
            enabled=False,
            log_level="INFO",
            agno_debug=False,
            env_file=self.config_status.env_file,
            env_file_exists=self.config_status.env_file.exists(),
        )

    def update_runtime_debug(
        self, request: RuntimeDebugUpdateRequest
    ) -> RuntimeDebugStatus:
        return RuntimeDebugStatus(
            enabled=request.enabled,
            log_level="DEBUG" if request.enabled else "INFO",
            agno_debug=request.enabled,
            env_file=self.config_status.env_file,
            env_file_exists=True,
        )

    def requires_github_for_routine(self, command: str) -> bool:
        return command == "workflows/ticket_driven_workflow"

    def verify(self) -> VerifyResponse:
        return VerifyResponse(
            ok=False,
            config=self.config_status,
            active_members=[],
            checks=[
                VerifyCheck(
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
            warnings=[],
            errors=[
                VerifyCheck(
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
        )

    async def run_scenario_diagnostics(
        self, person_id: str | None = None
    ) -> ScenarioDiagnosticsResponse:
        return ScenarioDiagnosticsResponse(
            ok=False,
            active_members=[person_id] if person_id else [],
            checks=[
                DiagnosticCheck(
                    section="members",
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
            warnings=[],
            errors=[
                DiagnosticCheck(
                    section="members",
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
        )

    def detect_cli_agents(self) -> CliAgentDetectionsResponse:
        return CliAgentDetectionsResponse(
            agents=[
                {
                    "name": "claude",
                    "executable": "claude",
                    "detected": True,
                    "path": "/usr/local/bin/claude",
                },
                {
                    "name": "codex",
                    "executable": "codex",
                    "detected": False,
                    "path": "",
                },
            ]
        )

    def is_github_integration_enabled(self) -> bool:
        return False


def test_health_requires_session_token(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        unauthorized = client.get("/health")
        response = client.get(
            "/health", headers={"X-GuildBotics-Session-Token": "secret"}
        )

    assert unauthorized.status_code == HTTP_UNAUTHORIZED
    assert unauthorized.json() == {
        "code": "invalid_session_token",
        "message": "Invalid session token.",
        "context": {},
    }
    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok"}


def test_workspace_change_updates_runtime_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/workspace",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"workspace_dir": str(workspace)},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["cwd"] == str(workspace)
    assert response.json()["config_dir"] == str(workspace / ".guildbotics/config")


def test_runtime_config_status_reports_workspace_active_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    project_file = tmp_path / ".guildbotics/config/team/project.yml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text("language: en\n")

    status = AppRuntime(EventBus()).get_config_status()

    assert status.config_dir == tmp_path / ".guildbotics/config"
    assert status.project_file_exists is True


def test_app_runtime_command_options_describe_workspace_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    command_file = tmp_path / ".guildbotics/config/commands/workflows/demo.py"
    command_file.parent.mkdir(parents=True)
    command_file.write_text(
        "\n".join(
            [
                "from guildbotics.integrations.ticket_manager import TicketManager",
                "",
                "async def main(context, title, *, dry_run='False'):",
                '    """Run a demo workflow."""',
                "    return title",
            ]
        )
    )

    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type(
        "ProjectStub",
        (),
        {
            "get_language_code": lambda self: "en",
            "is_available_service": lambda self, service: False,
        },
    )()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    response = runtime.get_command_options()
    option = next(item for item in response.options if item.command == "workflows/demo")

    assert option.label == "Demo"
    assert option.description == "Run a demo workflow."
    assert option.category == "workflow"
    assert [argument.name for argument in option.arguments] == ["title", "dry_run"]
    assert option.requirements[0].kind == "github"
    assert option.requirements[0].satisfied is False


def test_app_runtime_command_options_exclude_template_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type("ProjectStub", (), {"get_language_code": lambda self: "en"})()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    response = runtime.get_command_options()

    assert response.options == []


def test_app_runtime_command_options_seed_empty_workspace_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    project_file = tmp_path / ".guildbotics/config/team/project.yml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text("language: ja\n")
    commands_dir = tmp_path / ".guildbotics/config/commands"
    commands_dir.mkdir(parents=True)
    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type("ProjectStub", (), {"get_language_code": lambda self: "ja"})()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    response = runtime.get_command_options()

    assert (commands_dir / "translate.md").exists()
    assert {option.command for option in response.options} >= {
        "translate",
        "summarize",
        "get-time-of-day",
        "context-info",
    }
    requirements = {
        option.command: {requirement.kind for requirement in option.requirements}
        for option in response.options
    }
    assert requirements["get-time-of-day"] == {"llm"}
    assert requirements["summarize"] == {"cli_agent"}
    assert requirements["context-info"] == set()


def test_app_runtime_command_options_propagate_nested_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    commands_dir = tmp_path / ".guildbotics/config/commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "cli-task.md").write_text(
        "\n".join(["---", "brain: cli", "---", "Summarize ${file}."])
    )
    (commands_dir / "nested.yml").write_text(
        "\n".join(
            [
                "commands:",
                "  - command: cli-task file=README.md",
                "  - prompt: Explain the summary.",
            ]
        )
    )
    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type("ProjectStub", (), {"get_language_code": lambda self: "en"})()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    nested = next(
        option
        for option in runtime.get_command_options().options
        if option.command == "nested"
    )

    assert {requirement.kind for requirement in nested.requirements} == {
        "cli_agent",
        "llm",
    }


def test_app_runtime_command_options_resolve_brain_mapping_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    config_dir = tmp_path / ".guildbotics/config"
    commands_dir = config_dir / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "edit.md").write_text(
        "\n".join(["---", "brain: file_editor", "---", "Edit ${file}."])
    )
    brain_mapping = config_dir / "intelligences/brain_mapping.yml"
    brain_mapping.parent.mkdir(parents=True)
    brain_mapping.write_text(
        "\n".join(
            [
                "default:",
                "  class: guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain",
                "file_editor:",
                "  class: guildbotics.intelligences.brains.cli_agent.CliAgentBrain",
            ]
        )
    )
    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type("ProjectStub", (), {"get_language_code": lambda self: "en"})()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    option = runtime.get_command_options().options[0]

    assert {requirement.kind for requirement in option.requirements} == {"cli_agent"}


def test_app_runtime_command_options_extract_markdown_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    command_file = tmp_path / ".guildbotics/config/commands/translate.md"
    command_file.parent.mkdir(parents=True)
    command_file.write_text(
        "\n".join(
            [
                "---",
                "description: Translate ${1} to ${target}.",
                "---",
                "If this is ${1}, translate it into ${2}. {{ extra_note }}",
            ]
        )
    )
    person = type("PersonStub", (), {"person_id": "bot", "name": "Bot"})()
    project = type("ProjectStub", (), {"get_language_code": lambda self: "en"})()
    team = type("TeamStub", (), {"project": project, "members": [person]})()
    context = type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": lambda self, selected: self,
        },
    )()
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    option = runtime.get_command_options().options[0]

    assert [(arg.name, arg.kind) for arg in option.arguments] == [
        ("1", "positional"),
        ("2", "positional"),
        ("extra_note", "keyword"),
        ("target", "keyword"),
    ]


def test_command_run_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/commands/run",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"command": "hello"},
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "trace_id": "stub-request",
        "output": "ran hello",
    }


def test_command_options_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/commands/options",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["options"][0]["command"] == "hello"
    assert response.json()["options"][0]["label"] == "Hello"


def test_routine_command_options_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/commands/routine-options",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    commands = [option["command"] for option in payload["options"]]
    assert "hello" in commands
    assert "workflows/ticket_driven_workflow" in commands
    assert payload["default_command"] == "workflows/ticket_driven_workflow"


def test_event_stream_replays_trace_id(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = create_app(
        session_token="secret",
        runtime=RuntimeStub(tmp_path),
        event_bus=event_bus,
    )
    with trace_scope("manual", trace_id="request-1"):
        event_bus.publish_event(
            "command.started",
            {"command": "hello"},
        )

    with (
        TestClient(app) as client,
        client.websocket_connect("/events?token=secret") as websocket,
    ):
        event = websocket.receive_json()

    assert event["type"] == "command.started"
    assert event["trace_id"] == "request-1"
    assert event["payload"] == {"command": "hello"}
    assert event["timestamp"]


def test_command_error_uses_stable_error_shape(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/commands/run",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"command": "missing"},
        )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "command_error",
        "message": "Unable to locate command 'missing'.",
        "context": {"command": "missing"},
    }


def test_scheduler_start_event_queue_source_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/scheduler/start",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "sources": {"scheduled": False, "routine": False, "event_queue": True}
            },
        )

    assert response.status_code == HTTP_OK
    assert response.json()["events"]["state"] == "running"
    assert response.json()["scheduler"]["state"] == "running"


def test_prompt_trace_status_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))
    read_path = tmp_path / "old_trace.jsonl"

    with TestClient(app) as client:
        response = client.get(
            "/prompt-trace",
            headers={"X-GuildBotics-Session-Token": "secret"},
            params={"path": str(read_path)},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["enabled"] is False
    assert response.json()["trace_file"] == str(read_path)
    assert response.json()["output_trace_file"] == str(
        tmp_path / "home/.guildbotics/data/run/prompt_trace.jsonl"
    )
    assert response.json()["event_count"] == 0


def test_prompt_trace_update_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))
    trace_path = tmp_path / "trace.jsonl"

    with TestClient(app) as client:
        response = client.put(
            "/prompt-trace",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"enabled": True, "trace_path": str(trace_path)},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["enabled"] is True
    assert response.json()["trace_file"] == str(trace_path)
    assert response.json()["output_trace_file"] == str(trace_path)


def test_validation_error_uses_stable_error_shape(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/scheduler/start",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "sources": {"scheduled": False, "routine": False, "event_queue": False}
            },
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert payload["message"] == "Request validation failed."
    assert isinstance(payload["context"].get("errors"), list)


def test_cli_agent_detection_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/intelligences/cli-agents/detection",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["agents"][0]["name"] == "claude"
    assert payload["agents"][0]["detected"] is True
    assert payload["agents"][1]["name"] == "codex"
    assert payload["agents"][1]["detected"] is False


def test_scenario_diagnostics_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/diagnostics/scenario",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["section"] == "members"
    assert payload["errors"][0]["code"] == "active_members"


def test_scenario_diagnostics_endpoint_accepts_person_id(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/diagnostics/scenario?person_id=alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["active_members"] == ["alice"]


def test_config_init_endpoint_writes_project_without_github(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"

    with TestClient(app) as client:
        response = client.post(
            "/config/init",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file_path),
                "env_file_option": "overwrite",
                "language": "en",
                "description": "Local automation workspace",
                "llm_api_type": "openai",
                "cli_agent": "codex",
                "provider_api_keys": {"openai": "test-openai-key"},
            },
        )

    assert response.status_code == HTTP_OK
    assert (config_dir / "team/project.yml").exists()
    assert "test-openai-key" not in response.text
    assert "OPENAI_API_KEY=test-openai-key" in env_file_path.read_text()


def test_config_project_endpoints_read_and_update_non_destructively(
    tmp_path: Path,
) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    team_dir = config_dir / "team"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.yml").write_text(
        "\n".join(
            [
                "language: en",
                "description: Existing description",
                "repositories:",
                "  - name: GuildBotics",
                "services:",
                "  ticket_manager:",
                "    name: GitHub",
                "    owner: GuildBotics",
                "    project_id: '7'",
                "    url: https://github.com/orgs/GuildBotics/projects/7",
                "  code_hosting_service:",
                "    name: GitHub",
                "    owner: GuildBotics",
                "    repo_base_url: https://github.com",
            ]
        )
    )
    runtime.config_status.project_file_exists = True
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=existing-openai\nEXTRA=keep")
    model_mapping = config_dir / "intelligences/model_mapping.yml"
    model_mapping.parent.mkdir(parents=True, exist_ok=True)
    model_mapping.write_text(
        "\n".join(
            [
                "default: models/openai/gpt-5-mini.yml",
                "openai: models/openai/gpt-5-mini.yml",
                "gemini: models/gemini/gemini-3-flash.yml",
                "anthropic: models/anthropic/claude-haiku-4.yml",
            ]
        )
    )
    cli_mapping = config_dir / "intelligences/cli_agent_mapping.yml"
    cli_mapping.write_text(
        "\n".join(
            [
                "default: claude-cli.yml",
                "codex: codex-cli.yml",
                "antigravity: antigravity-cli.yml",
                "claude: claude-cli.yml",
                "copilot: copilot-cli.yml",
            ]
        )
    )

    with TestClient(app) as client:
        get_response = client.get(
            "/config/project",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["language"] == "en"
        assert payload["llm_api_type"] == "openai"
        assert payload["cli_agent"] == "claude"
        assert payload["provider_api_keys"]["openai"] is True
        assert payload["provider_api_keys"]["gemini"] is False

        put_response = client.put(
            "/config/project",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "language": "ja",
                "description": "Updated description",
                "llm_api_type": "gemini",
                "cli_agent": "codex",
                "github_enabled": True,
                "owner": "GuildBotics",
                "project_id": "7",
                "github_project_url": "https://github.com/orgs/GuildBotics/projects/7",
            },
        )
        assert put_response.status_code == HTTP_OK

    updated_project = safe_load((team_dir / "project.yml").read_text())
    assert updated_project["language"] == "ja"
    assert updated_project["description"] == "Updated description"
    code_hosting = updated_project["services"]["code_hosting_service"]
    assert code_hosting["owner"] == "GuildBotics"
    # Clone access is always HTTPS now, so the stale repo_base_url seeded in the
    # original file is dropped on update.
    assert "repo_base_url" not in code_hosting
    # ``repositories`` is no longer part of the schema; a stale entry seeded in
    # the original file is dropped on update.
    assert "repositories" not in updated_project

    env_text = env_file.read_text()
    assert "OPENAI_API_KEY=existing-openai" in env_text
    assert "EXTRA=keep" in env_text


def test_config_members_resolve_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    class ReferenceStub:
        def model_dump(self) -> dict[str, Any]:
            return {
                "person_id": "alice",
                "github_username": "alice",
                "github_user_id": 123,
                "git_email": "123+alice@users.noreply.github.com",
            }

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimplePersonSetupService.resolve_github_user",
        lambda self, identity, is_github_apps=False: ReferenceStub(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/config/members/resolve",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"person_type": "machine_user", "identity": "alice"},
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "person_id": "alice",
        "github_username": "alice",
        "github_user_id": 123,
        "git_email": "123+alice@users.noreply.github.com",
    }


def test_roles_endpoint_returns_template_roles(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/config/roles?language=ja",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert isinstance(payload["roles"], list)
    role_ids = [role["role_id"] for role in payload["roles"]]
    assert "architect" in role_ids
    assert "product" in role_ids
    assert "customer_success" in role_ids
    assert "growth" in role_ids
    assert "operations" in role_ids
    assert "programmer" not in role_ids
    assert "professional" not in role_ids
    assert "personal" not in role_ids


def test_intelligence_config_endpoints_read_update_and_member_inherit(
    tmp_path: Path,
) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    (config_dir / "team").mkdir(parents=True, exist_ok=True)
    (config_dir / "team/project.yml").write_text("language: en")
    runtime.config_status.project_file_exists = True

    with TestClient(app) as client:
        get_response = client.get(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["model_mapping"]["default"].startswith("models/")
        assert payload["brain_mapping"][0]["name"] == "default"
        default_cli_path = payload["cli_agent_mapping"]["default"]
        assert any(agent["path"] == default_cli_path for agent in payload["cli_agents"])

        payload["model_mapping"]["default"] = "models/openai/gpt-5-mini.yml"
        payload["brain_mapping"][0] = {
            "name": "default",
            "brain_class": "guildbotics.intelligences.brains.cli_agent.CliAgentBrain",
            "engine": "cli",
            "target": "codex",
        }
        update_response = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "model_mapping": payload["model_mapping"],
                "models": payload["models"],
                "cli_agent_mapping": payload["cli_agent_mapping"],
                "cli_agents": payload["cli_agents"],
                "brain_mapping": payload["brain_mapping"],
            },
        )
        assert update_response.status_code == HTTP_OK

        member_response = client.get(
            "/config/intelligences?person_id=alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert member_response.status_code == HTTP_OK
        member_payload = member_response.json()
        assert member_payload["inherited"] is True
        assert (
            member_payload["model_mapping"]["default"] == "models/openai/gpt-5-mini.yml"
        )

        member_payload["brain_mapping"][0] = {
            "name": "default",
            "brain_class": "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain",
            "engine": "llm",
            "target": "anthropic",
        }
        member_update = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "person_id": "alice",
                "model_mapping": member_payload["model_mapping"],
                "models": member_payload["models"],
                "cli_agent_mapping": member_payload["cli_agent_mapping"],
                "cli_agents": member_payload["cli_agents"],
                "brain_mapping": member_payload["brain_mapping"],
            },
        )
        assert member_update.status_code == HTTP_OK
        member_intelligences_dir = config_dir / "team/members/alice/intelligences"
        assert (
            safe_load((member_intelligences_dir / "model_mapping.yml").read_text())
            == member_payload["model_mapping"]
        )
        assert (
            safe_load((member_intelligences_dir / "cli_agent_mapping.yml").read_text())
            == member_payload["cli_agent_mapping"]
        )
        assert sorted(
            path.relative_to(member_intelligences_dir).as_posix()
            for path in member_intelligences_dir.rglob("*")
            if path.is_file()
        ) == ["cli_agent_mapping.yml", "model_mapping.yml"]

        inherit_reset = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "person_id": "alice",
                "inherit_team_defaults": True,
            },
        )
        assert inherit_reset.status_code == HTTP_OK

    brain_mapping = safe_load(
        (config_dir / "intelligences/brain_mapping.yml").read_text()
    )
    assert brain_mapping["default"]["class"] == (
        "guildbotics.intelligences.brains.cli_agent.CliAgentBrain"
    )
    assert not (config_dir / "team/members/alice/intelligences").exists()


def test_member_config_endpoints_read_update_delete(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    member_dir = config_dir / "team/members/alice"
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "person.yml").write_text(
        "\n".join(
            [
                "person_id: alice",
                "name: Alice Bot",
                "is_active: True",
                "person_type: machine_user",
                "account_info:",
                "  github_username: alice",
                "  git_user: Alice Bot",
                "  git_email: 123+alice@users.noreply.github.com",
                "profile:",
                "  roles:",
                "    architect: {}",
                "  character:",
                "    archetype: strategic_project_manager_architect",
                "    traits:",
                "      - strategic",
                "speaking_style: concise",
                "relationships: team lead",
                "message_channels:",
                "  - name: C012345",
                "    service: slack",
                "    chat:",
                "      enabled: True",
                "      participation: social",
            ]
        )
    )
    team_dir = config_dir / "team"
    (team_dir / "project.yml").parent.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.yml").write_text("language: en")
    runtime.config_status.project_file_exists = True
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ALICE_GITHUB_ACCESS_TOKEN=token-a",
                "ALICE_SLACK_BOT_TOKEN=xoxb-a",
                "ALICE_SLACK_APP_TOKEN=xapp-a",
            ]
        )
    )

    with TestClient(app) as client:
        get_response = client.get(
            "/config/members/alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["person_id"] == "alice"
        assert payload["roles"] == ["architect"]
        assert (
            payload["character"]["archetype"] == "strategic_project_manager_architect"
        )
        assert payload["has_github_access_token"] is True
        assert payload["has_slack_bot_token"] is True
        assert payload["slack_channels"] == ["C012345"]
        assert payload["slack_channel_participation"] == {"C012345": "social"}

        put_response = client.put(
            "/config/members/alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "original_person_id": "alice",
                "person_type": "machine_user",
                "person_id": "alice-renamed",
                "person_name": "Alice Updated",
                "is_active": False,
                "github_username": "alice-renamed",
                "git_email": "123+alice-renamed@users.noreply.github.com",
                "roles": ["reviewer"],
                "speaking_style": "updated",
                "relationships": "updated",
                "character": {
                    "archetype": "creative_designer",
                    "traits": ["creative", "playful"],
                    "interests": ["anime"],
                    "conversation_preferences": {
                        "join_when": ["ux discussion"],
                        "avoid_when": ["off-topic"],
                        "contribution_style": ["user perspective"],
                    },
                },
                "github_access_token": "token-b",
                "slack_bot_token": "xoxb-b",
                "slack_app_token": "xapp-b",
                "slack_channels": ["C0999"],
                "slack_channel_participation": {"C0999": "muted"},
            },
        )
        assert put_response.status_code == HTTP_OK

        renamed_file = config_dir / "team/members/alice-renamed/person.yml"
        updated = safe_load(renamed_file.read_text())
        assert updated["person_id"] == "alice-renamed"
        assert updated["name"] == "Alice Updated"
        assert "reviewer" in updated["profile"]["roles"]
        assert updated["profile"]["character"]["archetype"] == "creative_designer"
        assert updated["message_channels"][0]["name"] == "C0999"
        assert updated["message_channels"][0]["chat"]["participation"] == "muted"
        env_text = env_file.read_text()
        assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_text
        assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=token-b" in env_text
        assert "ALICE_RENAMED_SLACK_BOT_TOKEN=xoxb-b" in env_text

        delete_response = client.request(
            "DELETE",
            "/config/members/alice-renamed",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
            },
        )
        assert delete_response.status_code == HTTP_OK
    assert not (config_dir / "team/members/alice-renamed/person.yml").exists()
    env_text_after_delete = env_file.read_text()
    assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=token-b" not in env_text_after_delete
    assert "ALICE_RENAMED_SLACK_BOT_TOKEN" not in env_text_after_delete


def test_member_create_uses_existing_runtime_config_dir(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    (config_dir / "team").mkdir(parents=True, exist_ok=True)
    (config_dir / "team/project.yml").write_text("language: en")
    runtime.config_status.project_file_exists = True

    wrong_config_dir = tmp_path / "wrong/.guildbotics/config"
    wrong_env_file = tmp_path / "wrong/.env"
    with TestClient(app) as client:
        response = client.post(
            "/config/members",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(wrong_config_dir),
                "env_file_path": str(wrong_env_file),
                "person_type": "",
                "person_id": "new_member",
                "person_name": "New Member",
                "is_active": True,
                "github_username": "",
                "git_email": "",
                "roles": ["architect"],
            },
        )
    assert response.status_code == HTTP_OK
    assert (config_dir / "team/members/new_member/person.yml").exists()
    assert not (wrong_config_dir / "team/members/new_member/person.yml").exists()


def test_member_config_accepts_member_without_github_link(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"

    with TestClient(app) as client:
        response = client.post(
            "/config/members",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "person_type": "",
                "person_id": "local-agent",
                "person_name": "Local Agent",
                "is_active": True,
                "github_username": "",
                "git_email": "",
                "roles": ["architect"],
                "speaking_style": "concise",
            },
        )

    assert response.status_code == HTTP_OK
    person_config = safe_load(
        (config_dir / "team/members/local-agent/person.yml").read_text()
    )
    assert person_config["person_id"] == "local-agent"
    assert "person_type" not in person_config
    assert person_config["account_info"] == {"git_user": "Local Agent"}
    assert not env_file.exists()


def test_app_runtime_reports_missing_config(monkeypatch) -> None:
    class MissingConfigEdition:
        def get_context(self, message: str = ""):
            raise FileNotFoundError(2, "No such file", "project.yml")

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: MissingConfigEdition(),
    )

    runtime = AppRuntime(EventBus())

    with pytest.raises(AppApiError) as exc_info:
        runtime.get_team_summary()

    assert exc_info.value.code == "config_not_found"
    assert exc_info.value.context == {"path": "project.yml"}


def test_app_runtime_reload_workspace_env_before_context(monkeypatch, tmp_path) -> None:
    class ProjectStub:
        name = "Project"

        def get_language_code(self) -> str:
            return "ja"

        def get_language_name(self) -> str:
            return "日本語"

    class ContextStub:
        team = type(
            "TeamStub",
            (),
            {
                "project": ProjectStub(),
                "members": [],
            },
        )()

    class EditionStub:
        def get_context(self, message: str = "") -> object:
            assert os.environ["OPENAI_API_KEY"] == "new-key"
            return ContextStub()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=new-key\n")
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: EditionStub(),
    )

    runtime = AppRuntime(EventBus())

    runtime.get_team_summary()


def test_app_runtime_updates_prompt_trace_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE", raising=False)
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE_PATH", raising=False)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        '{"event":"llm.request","timestamp":"2026-06-01T12:00:00+09:00",'
        '"person_id":"alice","brain":"default","message":"hello"}\n'
    )
    runtime = AppRuntime(EventBus())

    status = runtime.update_prompt_trace(
        PromptTraceUpdateRequest(enabled=True, trace_path=str(trace_path))
    )

    assert os.environ["GUILDBOTICS_PROMPT_TRACE"] == "1"
    assert os.environ["GUILDBOTICS_PROMPT_TRACE_PATH"] == str(trace_path)
    assert "GUILDBOTICS_PROMPT_TRACE=1" in (tmp_path / ".env").read_text()
    assert status.enabled is True
    assert status.trace_file == trace_path
    assert status.event_count == 1
    assert status.events[0].person_id == "alice"
    assert status.events[0].prompt == "hello"


def test_app_runtime_formats_prompt_trace_description_and_transcript(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        '{"event":"chat.reply_input","timestamp":"2026-06-01T12:00:00+09:00",'
        '"person_id":"alice","transcript":"one\\ntwo",'
        '"payload":{"thread_messages":[{"author":"alice","content":"hello"}]}}\n'
        '{"event":"llm.request","timestamp":"2026-06-01T12:00:01+09:00",'
        '"person_id":"alice","brain":"functions/answer",'
        '"description":"system\\nprompt","message":"hello"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))
    runtime = AppRuntime(EventBus())

    status = runtime.get_prompt_trace_status()

    assert status.events[0].description == "system\nprompt"
    assert status.events[0].fields.get("description") is None
    assert status.events[1].transcript == "one\ntwo"
    assert status.events[1].fields.get("transcript") is None


def test_app_runtime_formats_structured_prompt_trace_response(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        '{"event":"llm.response","timestamp":"2026-06-01T12:00:00+09:00",'
        '"person_id":"alice","brain":"functions/answer",'
        '"content":{"status":"ok","message":"こんにちは"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))
    runtime = AppRuntime(EventBus())

    status = runtime.get_prompt_trace_status()

    assert status.event_count == 1
    assert status.events[0].response == (
        '{\n  "message": "こんにちは",\n  "status": "ok"\n}'
    )


def test_app_runtime_scheduler_start_stop_lifecycle(monkeypatch) -> None:
    class EditionStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    default_stop_timeout = 10.0
    started = threading.Event()
    release = threading.Event()

    class BlockingScheduler:
        instances: ClassVar[list["BlockingScheduler"]] = []

        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
            routine_interval_minutes: int,
            service_run_id: str | None = None,
            scheduled_source_enabled: bool = True,
            routine_source_enabled: bool = True,
            event_queue_source_enabled: bool = True,
        ) -> None:
            self.shutdown_calls = 0
            self.routine_commands = routine_commands
            self.consecutive_error_limit = consecutive_error_limit
            self.routine_interval_minutes = routine_interval_minutes
            self.scheduled_source_enabled = scheduled_source_enabled
            self.routine_source_enabled = routine_source_enabled
            self.event_queue_source_enabled = event_queue_source_enabled
            BlockingScheduler.instances.append(self)

        def start(self) -> None:
            started.set()
            release.wait(THREAD_WAIT_SECONDS)

        def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
            self.shutdown_calls += 1
            self.shutdown_timeout = timeout
            release.set()

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: EditionStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        BlockingScheduler,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=default_stop_timeout)

    first = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    assert first.scheduler.state == "running"
    assert first.scheduler.routine_commands == []
    assert first.scheduler.max_consecutive_errors == DEFAULT_MAX_CONSECUTIVE_ERRORS
    assert first.scheduler.routine_interval_minutes == DEFAULT_ROUTINE_INTERVAL_MINUTES
    assert (
        BlockingScheduler.instances[0].routine_interval_minutes
        == DEFAULT_ROUTINE_INTERVAL_MINUTES
    )
    assert started.wait(THREAD_WAIT_SECONDS)

    second = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    assert second.scheduler.state == "running"
    assert len(BlockingScheduler.instances) == 1

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "stopped"
    assert BlockingScheduler.instances[0].shutdown_calls == 1
    assert BlockingScheduler.instances[0].shutdown_timeout == default_stop_timeout

    stopped_again = runtime.stop_scheduler()
    assert stopped_again.scheduler.state == "stopped"
    assert BlockingScheduler.instances[0].shutdown_calls == 1


def test_app_runtime_marks_scheduler_failed_on_stop_timeout(monkeypatch) -> None:
    class EditionStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    started = threading.Event()
    release = threading.Event()

    class StuckScheduler:
        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
            routine_interval_minutes: int,
            service_run_id: str | None = None,
            scheduled_source_enabled: bool = True,
            routine_source_enabled: bool = True,
            event_queue_source_enabled: bool = True,
        ) -> None:
            self.shutdown_timeout: float | None = None

        def start(self) -> None:
            started.set()
            release.wait(THREAD_WAIT_SECONDS)

        def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
            self.shutdown_timeout = timeout

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: EditionStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        StuckScheduler,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=0.01)

    first = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    assert first.scheduler.state == "running"
    assert started.wait(THREAD_WAIT_SECONDS)

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "failed"
    assert stopped.scheduler.running is True
    assert stopped.scheduler.error == "Scheduler did not stop before timeout."
    release.set()


def test_app_runtime_event_listener_start_stop_lifecycle(monkeypatch) -> None:
    class EditionStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    class RunningEventListener:
        instances: ClassVar[list["RunningEventListener"]] = []

        def __init__(self, context: object, service_run_id: str | None = None) -> None:
            self.alive = False
            self.stop_calls = 0
            RunningEventListener.instances.append(self)

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.stop_calls += 1
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return self.alive

    scheduler_release = threading.Event()

    class RunningScheduler:
        instances: ClassVar[list["RunningScheduler"]] = []

        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
            routine_interval_minutes: int,
            service_run_id: str | None = None,
            scheduled_source_enabled: bool = True,
            routine_source_enabled: bool = True,
            event_queue_source_enabled: bool = True,
        ) -> None:
            self.routine_commands = routine_commands
            self.scheduled_source_enabled = scheduled_source_enabled
            self.routine_source_enabled = routine_source_enabled
            self.event_queue_source_enabled = event_queue_source_enabled
            self.shutdown_calls = 0
            RunningScheduler.instances.append(self)

        def start(self) -> None:
            scheduler_release.wait(THREAD_WAIT_SECONDS)

        def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
            self.shutdown_calls += 1
            scheduler_release.set()

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: EditionStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        RunningScheduler,
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.EventListenerRunner",
        RunningEventListener,
    )

    runtime = AppRuntime(EventBus())

    first = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )
    assert first.scheduler.state == "running"
    assert first.events.state == "running"
    assert RunningScheduler.instances[0].routine_commands == []
    assert RunningScheduler.instances[0].scheduled_source_enabled is False
    assert RunningScheduler.instances[0].routine_source_enabled is False
    assert RunningScheduler.instances[0].event_queue_source_enabled is True

    second = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )
    assert second.scheduler.state == "running"
    assert second.events.state == "running"
    assert len(RunningEventListener.instances) == 1
    assert len(RunningScheduler.instances) == 1

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "stopped"
    assert stopped.events.state == "stopped"
    assert RunningScheduler.instances[0].shutdown_calls == 1
    assert RunningEventListener.instances[0].stop_calls == 1

    stopped_again = runtime.stop_scheduler()
    assert stopped_again.scheduler.state == "stopped"
    assert stopped_again.events.state == "stopped"
    assert RunningScheduler.instances[0].shutdown_calls == 1
    assert RunningEventListener.instances[0].stop_calls == 1


def test_app_runtime_marks_event_listener_failed_on_start_error(monkeypatch) -> None:
    class MissingConfigEdition:
        def get_context(self, message: str = "") -> object:
            raise FileNotFoundError(2, "No such file", "project.yml")

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: MissingConfigEdition(),
    )

    runtime = AppRuntime(EventBus())

    with pytest.raises(AppApiError) as exc_info:
        runtime.start_scheduler(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )

    assert exc_info.value.code == "config_not_found"
    status = runtime.get_scheduler_status()
    assert status.scheduler.state == "failed"
    assert status.events.state == "stopped"


def test_app_runtime_marks_event_listener_failed_on_stop_timeout(monkeypatch) -> None:
    class EditionStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    class StuckEventListener:
        instances: ClassVar[list["StuckEventListener"]] = []

        def __init__(self, context: object, service_run_id: str | None = None) -> None:
            self.alive = False
            StuckEventListener.instances.append(self)

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return self.alive

    scheduler_release = threading.Event()

    class RunningScheduler:
        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
            routine_interval_minutes: int,
            service_run_id: str | None = None,
            scheduled_source_enabled: bool = True,
            routine_source_enabled: bool = True,
            event_queue_source_enabled: bool = True,
        ) -> None:
            return

        def start(self) -> None:
            scheduler_release.wait(THREAD_WAIT_SECONDS)

        def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
            scheduler_release.set()

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition",
        lambda: EditionStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        RunningScheduler,
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.EventListenerRunner",
        StuckEventListener,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=0.01)

    started = runtime.start_scheduler(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )
    assert started.scheduler.state == "running"
    assert started.events.state == "running"

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "stopped"
    assert stopped.events.state == "failed"
    assert stopped.events.running is True
    assert stopped.events.error == "Event listener runner did not stop before timeout."

    StuckEventListener.instances[0].alive = False
    refreshed = runtime.get_scheduler_status()
    assert refreshed.events.state == "stopped"
    assert refreshed.events.running is False
    assert refreshed.events.error is None


def test_app_runtime_rejects_github_required_routine_without_integration() -> None:
    runtime = AppRuntime(EventBus())
    runtime.is_github_integration_enabled = lambda: False  # type: ignore[method-assign]
    runtime.requires_github_for_routine = lambda command: True  # type: ignore[method-assign]

    with pytest.raises(AppApiError) as exc_info:
        runtime.start_scheduler(
            SchedulerStartRequest(routine_commands=["workflows/ticket_driven_workflow"])
        )

    assert exc_info.value.code == "github_integration_required_for_routine"


def test_app_runtime_derives_github_requirement_from_routine_option() -> None:
    # The GitHub dependency is read from the routine command's own detected
    # requirements, not a hardcoded command name.
    runtime = AppRuntime(EventBus())
    ticket = CommandOption(
        command="workflows/ticket_driven_workflow",
        label="Ticket Driven Workflow",
        category="workflow",
        source="template",
        path=Path("templates/commands/workflows/ticket_driven_workflow.py"),
        requirements=[CommandRequirement(kind="github", satisfied=False)],
    )
    plain = CommandOption(
        command="workflows/local_only",
        label="Local Only",
        category="workflow",
        source="workspace",
        path=Path("commands/workflows/local_only.py"),
    )
    runtime.get_routine_command_options = (  # type: ignore[method-assign]
        lambda person=None: RoutineCommandOptionsResponse(
            options=[plain, ticket], default_command=ticket.command
        )
    )

    assert (
        runtime.requires_github_for_routine("workflows/ticket_driven_workflow") is True
    )
    assert runtime.requires_github_for_routine("workflows/local_only") is False


@pytest.mark.asyncio
async def test_app_runtime_rejects_parallel_commands(monkeypatch) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_command(*_: Any, **__: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

    running = asyncio.create_task(
        runtime.run_command(CommandRunRequest(command="first"))
    )
    await started.wait()

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="second"))

    release.set()
    response = await running

    assert response.output == "done"
    assert exc_info.value.code == "command_already_running"
    assert exc_info.value.status_code == HTTP_CONFLICT
    events = event_bus.snapshot_events()
    assert [event["type"] for event in events] == [
        "command.started",
        "command.finished",
    ]
    assert all(event["trace_id"] == response.trace_id for event in events)


@pytest.mark.asyncio
async def test_manual_command_activity_history_uses_resolved_default_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    event_bus = EventBus(store=store)
    runtime = AppRuntime(event_bus, diagnostics_store=store)
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
        ],
    )

    async def fake_run_command(*_: Any, **__: Any) -> str:
        return "done"

    monkeypatch.setattr(
        runtime,
        "_get_context",
        lambda message="": type("ContextStub", (), {"team": team})(),
    )
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

    await runtime.run_command(CommandRunRequest(command="functions/talk_as"))

    history = runtime.get_activity_history(
        start="2000-01-01T00:00:00Z",
        end="2999-01-01T00:00:00Z",
    )

    assert [member.person_id for member in history.members] == ["alice"]
    assert len(history.sessions) == 1
    assert history.sessions[0].person_id == "alice"
    assert history.sessions[0].mode == "interactive"
    assert history.sessions[0].title == "functions/talk_as"


# --- auth coverage -------------------------------------------------------


PROTECTED_ENDPOINTS = [
    ("GET", "/health"),
    ("GET", "/config/status"),
    ("POST", "/workspace"),
    ("GET", "/team"),
    ("GET", "/commands/options"),
    ("GET", "/commands/routine-options"),
    ("POST", "/commands/run"),
    ("GET", "/config/roles"),
    ("GET", "/scheduler/status"),
    ("POST", "/scheduler/start"),
    ("POST", "/scheduler/stop"),
    ("GET", "/prompt-trace"),
    ("PUT", "/prompt-trace"),
    ("GET", "/runtime/debug"),
    ("PUT", "/runtime/debug"),
    ("POST", "/verify"),
    ("POST", "/diagnostics/scenario"),
    ("GET", "/intelligences/cli-agents/detection"),
    ("GET", "/config/intelligences"),
    ("PUT", "/config/intelligences"),
    ("POST", "/config/init"),
    ("GET", "/config/project"),
    ("PUT", "/config/project"),
    ("POST", "/config/members"),
    ("GET", "/config/members/alice"),
    ("PUT", "/config/members/alice"),
    ("DELETE", "/config/members/alice"),
    ("POST", "/config/members/resolve"),
]


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
@pytest.mark.parametrize("headers", [None, {"X-GuildBotics-Session-Token": "wrong"}])
def test_every_endpoint_rejects_missing_or_invalid_token(
    tmp_path: Path, method: str, path: str, headers: dict[str, str] | None
) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.request(method, path, headers=headers, json={})

    assert response.status_code == HTTP_UNAUTHORIZED
    assert response.json() == {
        "code": "invalid_session_token",
        "message": "Invalid session token.",
        "context": {},
    }


def test_unexpected_error_maps_to_internal_error(tmp_path: Path) -> None:
    class BoomStub(RuntimeStub):
        def get_team_summary(self) -> TeamSummary:
            raise RuntimeError("boom")

    client = TestClient(
        create_app(session_token="secret", runtime=BoomStub(tmp_path)),
        raise_server_exceptions=False,
    )

    response = client.get("/team", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_INTERNAL_SERVER_ERROR
    assert response.json() == {
        "code": "internal_error",
        "message": "An unexpected app API error occurred.",
        "context": {"error_type": "RuntimeError"},
    }


# --- config / workspace --------------------------------------------------


def test_workspace_change_rejects_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    client = TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )

    response = client.post(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace_dir": str(missing)},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "workspace_not_found"
    assert payload["context"]["workspace_dir"] == str(missing.resolve())


def test_workspace_change_rejects_non_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")
    client = TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )

    response = client.post(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace_dir": str(file_path)},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "workspace_not_directory"
    assert payload["context"]["workspace_dir"] == str(file_path.resolve())


def test_config_roles_rejects_invalid_language(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/config/roles?language=fr", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert isinstance(payload["context"]["errors"], list)


def test_config_init_maps_setup_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(RuntimeStub(tmp_path))

    def _boom(self: SimpleProjectSetupService, request: Any) -> Any:
        raise SetupServiceError("project_invalid", "Project config is invalid.")

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimpleProjectSetupService.write_project", _boom
    )

    response = client.post(
        "/config/init",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(tmp_path / ".guildbotics/config"),
            "env_file_path": str(tmp_path / ".env"),
            "env_file_option": "overwrite",
            "language": "en",
            "description": "desc",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "provider_api_keys": {"openai": "k"},
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "project_invalid",
        "message": "Project config is invalid.",
        "context": {},
    }


def test_config_project_get_reports_project_not_found(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/config/project", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "project_not_found"
    assert payload["context"]["project"] == str(
        tmp_path / ".guildbotics/config/team/project.yml"
    )


def test_config_project_status_options_returns_payload(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    runtime.status_options = ProjectStatusOptionsResponse(
        available=True, statuses=["Todo", "In Progress", "Done"]
    )
    client = _client(runtime)

    response = client.post(
        "/config/project/status-options",
        headers=AUTH_HEADERS,
        json={
            "owner": "acme",
            "project_id": "9",
            "github_project_url": "https://github.com/orgs/acme/projects/9",
        },
    )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "available": True,
        "statuses": ["Todo", "In Progress", "Done"],
    }
    assert runtime.status_options_request.owner == "acme"
    assert runtime.status_options_request.project_id == "9"


def test_config_project_agent_field_returns_state(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    runtime.agent_field_state = AgentFieldStateResponse(
        available=True,
        exists=True,
        options=[AgentFieldOption(name="⚙bot1", description="Bot One")],
        missing=[AgentFieldOption(name="⚙bot2", description="Bot Two")],
    )
    client = _client(runtime)

    response = client.post(
        "/config/project/agent-field",
        headers=AUTH_HEADERS,
        json={
            "owner": "acme",
            "project_id": "9",
            "github_project_url": "https://github.com/orgs/acme/projects/9",
        },
    )

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["available"] is True
    assert body["exists"] is True
    assert body["options"] == [{"name": "⚙bot1", "description": "Bot One"}]
    assert body["missing"] == [{"name": "⚙bot2", "description": "Bot Two"}]
    assert runtime.agent_field_request.owner == "acme"


def test_config_project_agent_field_ensure_applies(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    runtime.agent_field_state = AgentFieldStateResponse(
        available=True, exists=True, options=[], missing=[]
    )
    client = _client(runtime)

    response = client.post(
        "/config/project/agent-field/ensure",
        headers=AUTH_HEADERS,
        json={
            "owner": "acme",
            "project_id": "9",
            "github_project_url": "https://github.com/orgs/acme/projects/9",
        },
    )

    assert response.status_code == HTTP_OK
    assert response.json()["available"] is True
    assert runtime.agent_field_ensure_request.project_id == "9"


def test_config_project_status_options_unavailable_falls_back(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.post(
        "/config/project/status-options",
        headers=AUTH_HEADERS,
        json={"owner": "", "project_id": "", "github_project_url": ""},
    )

    assert response.status_code == HTTP_OK
    assert response.json() == {"available": False, "statuses": []}


def test_config_project_update_maps_setup_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(RuntimeStub(tmp_path))

    def _boom(self: SimpleProjectSetupService, request: Any) -> Any:
        raise SetupServiceError("project_invalid", "Project config is invalid.")

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimpleProjectSetupService.update_project", _boom
    )

    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(tmp_path / ".guildbotics/config"),
            "env_file_path": str(tmp_path / ".env"),
            "language": "en",
            "description": "desc",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "github_enabled": False,
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "project_invalid"


# --- members -------------------------------------------------------------


def test_member_config_get_reports_project_not_found(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/config/members/alice", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "project_not_found"


def test_member_update_rejects_person_id_mismatch(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(tmp_path / ".guildbotics/config"),
            "env_file_path": str(tmp_path / ".env"),
            "original_person_id": "bob",
            "person_type": "machine_user",
            "person_id": "alice",
            "person_name": "Alice",
            "is_active": True,
            "github_username": "alice",
            "git_email": "1+alice@users.noreply.github.com",
            "roles": ["architect"],
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "person_id_mismatch",
        "message": "original_person_id must match the path parameter.",
        "context": {},
    }


def test_members_resolve_github_apps_url_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(RuntimeStub(tmp_path))
    captured: dict[str, Any] = {}

    def _parse(self: SimplePersonSetupService, url: str) -> str:
        captured["url"] = url
        return "my-app"

    def _resolve(
        self: SimplePersonSetupService, identity: str, is_github_apps: bool = False
    ) -> GitHubUserReference:
        captured["identity"] = identity
        captured["is_github_apps"] = is_github_apps
        return GitHubUserReference(
            person_id="my-app",
            github_username="my-app[bot]",
            github_user_id=GITHUB_APPS_USER_ID,
            git_email="42+my-app[bot]@users.noreply.github.com",
        )

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimplePersonSetupService.parse_github_apps_url",
        _parse,
    )
    monkeypatch.setattr(
        "guildbotics.app_api.api.SimplePersonSetupService.resolve_github_user",
        _resolve,
    )

    response = client.post(
        "/config/members/resolve",
        headers=AUTH_HEADERS,
        json={
            "person_type": "github_apps",
            "identity": "https://github.com/apps/my-app",
        },
    )

    assert response.status_code == HTTP_OK
    assert captured == {
        "url": "https://github.com/apps/my-app",
        "identity": "my-app",
        "is_github_apps": True,
    }
    assert response.json()["github_username"] == "my-app[bot]"
    assert response.json()["github_user_id"] == GITHUB_APPS_USER_ID


def test_members_resolve_maps_setup_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(RuntimeStub(tmp_path))

    def _boom(
        self: SimplePersonSetupService, identity: str, is_github_apps: bool = False
    ) -> GitHubUserReference:
        raise SetupServiceError("github_user_not_found", "GitHub user not found.")

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimplePersonSetupService.resolve_github_user",
        _boom,
    )

    response = client.post(
        "/config/members/resolve",
        headers=AUTH_HEADERS,
        json={"person_type": "machine_user", "identity": "ghost"},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "github_user_not_found",
        "message": "GitHub user not found.",
        "context": {},
    }


# --- intelligences -------------------------------------------------------


def test_config_intelligences_reports_project_not_found(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/config/intelligences", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "project_not_found"


def test_model_providers_available_before_project_init(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/intelligences/model-providers", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    providers = {entry["provider"]: entry for entry in response.json()["providers"]}
    assert providers["openai"]["api_key_env"] == "OPENAI_API_KEY"


def test_config_intelligences_update_maps_setup_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(RuntimeStub(tmp_path))

    def _boom(self: Any, request: Any) -> Any:
        raise SetupServiceError("intelligence_invalid", "Intelligence config invalid.")

    monkeypatch.setattr(
        "guildbotics.app_api.api.IntelligenceConfigService.update_config", _boom
    )

    response = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json={"config_dir": str(tmp_path / ".guildbotics/config")},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "intelligence_invalid",
        "message": "Intelligence config invalid.",
        "context": {},
    }


# --- scheduler -----------------------------------------------------------


def test_scheduler_start_scheduled_and_routine_sources_endpoint(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.post(
        "/scheduler/start",
        headers=AUTH_HEADERS,
        json={"sources": {"scheduled": True, "routine": True, "event_queue": False}},
    )

    assert response.status_code == HTTP_OK
    assert response.json()["scheduler"]["state"] == "running"
    assert response.json()["events"]["state"] == "stopped"


def test_scheduler_start_both_endpoint(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.post("/scheduler/start", headers=AUTH_HEADERS, json={})

    assert response.status_code == HTTP_OK
    assert response.json()["scheduler"]["state"] == "running"
    assert response.json()["events"]["state"] == "running"


def test_scheduler_start_rejects_github_required_routine(tmp_path: Path) -> None:
    class RejectingStub(RuntimeStub):
        def start_scheduler(self, request: Any) -> RuntimeStatus:
            raise AppApiError(
                "github_integration_required_for_routine",
                "GitHub integration is required for this routine.",
                context={"routine_commands": list(request.routine_commands)},
            )

    client = _client(RejectingStub(tmp_path))

    response = client.post(
        "/scheduler/start",
        headers=AUTH_HEADERS,
        json={"routine_commands": ["workflows/ticket_driven_workflow"]},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    payload = response.json()
    assert payload["code"] == "github_integration_required_for_routine"
    assert payload["context"]["routine_commands"] == [
        "workflows/ticket_driven_workflow"
    ]


def test_scheduler_stop_endpoint(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.post("/scheduler/stop", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    assert response.json()["scheduler"]["state"] == "stopped"
    assert response.json()["events"]["state"] == "stopped"


# --- prompt trace --------------------------------------------------------


@pytest.mark.parametrize("limit", [0, 1001])
def test_prompt_trace_get_rejects_out_of_range_limit(
    tmp_path: Path, limit: int
) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get(
        "/prompt-trace", headers=AUTH_HEADERS, params={"limit": limit}
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("limit", [0, 1001])
def test_prompt_trace_put_rejects_out_of_range_limit(
    tmp_path: Path, limit: int
) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.put(
        "/prompt-trace",
        headers=AUTH_HEADERS,
        params={"limit": limit},
        json={"enabled": True},
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_runtime_debug_status_endpoint(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.get("/runtime/debug", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["enabled"] is False
    assert body["log_level"] == "INFO"
    assert body["agno_debug"] is False


def test_runtime_debug_update_endpoint(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.put(
        "/runtime/debug",
        headers=AUTH_HEADERS,
        json={"enabled": True},
    )

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["enabled"] is True
    assert body["log_level"] == "DEBUG"
    assert body["agno_debug"] is True


# --- commands ------------------------------------------------------------


def test_command_options_reports_person_not_found(tmp_path: Path) -> None:
    class PersonStub(RuntimeStub):
        def get_command_options(
            self, person: str | None = None
        ) -> CommandOptionsResponse:
            raise AppApiError(
                "person_not_found",
                "Member not found.",
                context={"person": person},
            )

    client = _client(PersonStub(tmp_path))

    response = client.get(
        "/commands/options", headers=AUTH_HEADERS, params={"person": "ghost"}
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "person_not_found",
        "message": "Member not found.",
        "context": {"person": "ghost"},
    }


def test_command_run_conflict_returns_409(tmp_path: Path) -> None:
    class ConflictStub(RuntimeStub):
        async def run_command(self, request: Any) -> Any:
            raise AppApiError(
                "command_already_running",
                "A command is already running.",
                status_code=HTTP_CONFLICT,
            )

    client = _client(ConflictStub(tmp_path))

    response = client.post(
        "/commands/run", headers=AUTH_HEADERS, json={"command": "hello"}
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json() == {
        "code": "command_already_running",
        "message": "A command is already running.",
        "context": {},
    }


# --- diagnostics ---------------------------------------------------------


def test_verify_endpoint_uses_runtime(tmp_path: Path) -> None:
    client = _client(RuntimeStub(tmp_path))

    response = client.post("/verify", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "active_members"
    assert payload["checks"][0]["status"] == "error"
