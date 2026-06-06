from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class VerifyCheck(BaseModel):
    code: str
    status: str = Field(pattern="^(ok|warning|error)$")
    message: str
    target: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ApiError(BaseModel):
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str


ConfigLocation = Literal["workspace", "home", "custom"]
ActiveConfigLocation = Literal["workspace", "home", "custom", "missing"]


class ConfigStatus(BaseModel):
    cwd: Path
    env_file: Path
    env_file_exists: bool
    primary_config_dir: Path
    primary_config_location: ConfigLocation = "workspace"
    primary_project_file: Path
    primary_project_file_exists: bool
    home_config_dir: Path
    home_project_file: Path
    home_project_file_exists: bool
    active_config_dir: Path | None = None
    active_config_location: ActiveConfigLocation = "missing"
    storage_dir: Path


class WorkspaceChangeRequest(BaseModel):
    workspace_dir: Path


class ProjectSummary(BaseModel):
    name: str = ""
    language_code: str
    language_name: str


class MemberSummary(BaseModel):
    person_id: str
    name: str
    is_active: bool
    roles: list[str]


class TeamSummary(BaseModel):
    project: ProjectSummary
    members: list[MemberSummary]


class MemberDeleteRequest(BaseModel):
    config_dir: Path
    env_file_path: Path


class RoutineOption(BaseModel):
    command: str
    requires_github: bool


class RoutineOptionsResponse(BaseModel):
    routines: list[RoutineOption]


class CommandArgumentOption(BaseModel):
    name: str
    kind: Literal["positional", "keyword"]
    required: bool = False
    default: str = ""


class CommandRequirement(BaseModel):
    kind: Literal["github", "slack", "cli_agent", "llm"]
    satisfied: bool
    message: str = ""


class CommandOption(BaseModel):
    command: str
    label: str
    description: str = ""
    category: Literal["workflow", "function", "example", "custom"]
    source: Literal["workspace", "home", "template"]
    path: Path
    arguments: list[CommandArgumentOption] = Field(default_factory=list)
    supports_raw_args: bool = True
    recommended_input: str = ""
    requirements: list[CommandRequirement] = Field(default_factory=list)


class CommandOptionsResponse(BaseModel):
    options: list[CommandOption]


class RoleOption(BaseModel):
    role_id: str
    summary: str = ""
    description: str = ""


class RoleOptionsResponse(BaseModel):
    roles: list[RoleOption]


class CommandRunRequest(BaseModel):
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    person: str | None = None
    message: str = ""
    cwd: Path | None = None


class CommandRunResponse(BaseModel):
    request_id: str
    output: str


class SchedulerStartRequest(BaseModel):
    only: str | None = Field(default=None, pattern="^(scheduler|events)$")
    routine_commands: list[str] = Field(default_factory=list)
    max_consecutive_errors: int = Field(default=3, ge=1)
    routine_interval_minutes: int = Field(default=10, ge=1)


class RuntimeUnitStatus(BaseModel):
    target: Literal["scheduler", "events"]
    state: Literal["starting", "running", "stopping", "stopped", "failed"]
    running: bool
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None
    routine_commands: list[str] = Field(default_factory=list)
    max_consecutive_errors: int | None = None
    routine_interval_minutes: int | None = None
    active_member_count: int | None = None
    worker_count: int | None = None
    workflow_command: str | None = None
    subscription_count: int | None = None
    listener_count: int | None = None
    cycle_count: int | None = None
    cycle_failure_count: int | None = None
    events_drained_count: int | None = None
    events_delivered_count: int | None = None
    events_skipped_processed_count: int | None = None


class RuntimeStatus(BaseModel):
    scheduler: RuntimeUnitStatus
    events: RuntimeUnitStatus


class PromptTraceUpdateRequest(BaseModel):
    enabled: bool
    trace_path: str = ""


class PromptTraceEntry(BaseModel):
    event: str
    timestamp: str = ""
    person_id: str = ""
    brain: str = ""
    command: str = ""
    target: str = ""
    cwd: str = ""
    description: str = ""
    transcript: str = ""
    prompt: str = ""
    response: str = ""
    error: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)


class PromptTraceStatus(BaseModel):
    enabled: bool
    env_file: Path
    env_file_exists: bool
    trace_file: Path
    output_trace_file: Path
    default_trace_file: Path
    trace_file_exists: bool
    event_count: int
    events: list[PromptTraceEntry] = Field(default_factory=list)


class VerifyResponse(BaseModel):
    ok: bool
    config: ConfigStatus
    active_members: list[str]
    checks: list[VerifyCheck]
    warnings: list[VerifyCheck]
    errors: list[VerifyCheck]


class DiagnosticCheck(BaseModel):
    section: Literal["config", "members", "llm", "cli_agent", "github", "slack", "git"]
    code: str
    status: Literal["ok", "warning", "error"]
    message: str
    target: str = ""
    person_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ScenarioDiagnosticsResponse(BaseModel):
    ok: bool
    active_members: list[str]
    checks: list[DiagnosticCheck]
    warnings: list[DiagnosticCheck]
    errors: list[DiagnosticCheck]


class CliAgentDetection(BaseModel):
    name: Literal["codex", "gemini", "claude", "copilot"]
    executable: str
    detected: bool
    path: str = ""


class CliAgentDetectionsResponse(BaseModel):
    agents: list[CliAgentDetection]


class ModelDefinition(BaseModel):
    path: str
    provider: str
    model_class: str = ""
    model_id: str = ""


class CliAgentDefinition(BaseModel):
    path: str
    name: str
    env: dict[str, Any] = Field(default_factory=dict)
    script: str = ""
    detected: bool = False
    detected_path: str = ""


class BrainAssignment(BaseModel):
    name: str
    brain_class: str
    engine: Literal["llm", "cli"]
    target: str


class IntelligenceConfigResponse(BaseModel):
    config_dir: Path
    person_id: str | None = None
    inherited: bool = False
    model_mapping: dict[str, str] = Field(default_factory=dict)
    models: list[ModelDefinition] = Field(default_factory=list)
    cli_agent_mapping: dict[str, str] = Field(default_factory=dict)
    cli_agents: list[CliAgentDefinition] = Field(default_factory=list)
    brain_mapping: list[BrainAssignment] = Field(default_factory=list)


class IntelligenceConfigUpdateRequest(BaseModel):
    config_dir: Path
    person_id: str | None = None
    inherit_team_defaults: bool = False
    model_mapping: dict[str, str] = Field(default_factory=dict)
    models: list[ModelDefinition] = Field(default_factory=list)
    cli_agent_mapping: dict[str, str] = Field(default_factory=dict)
    cli_agents: list[CliAgentDefinition] = Field(default_factory=list)
    brain_mapping: list[BrainAssignment] = Field(default_factory=list)


class ProjectConfigResponse(BaseModel):
    config_dir: Path
    env_file_path: Path
    language: str
    description: str = ""
    llm_api_type: Literal["openai", "gemini", "anthropic"]
    cli_agent: Literal["codex", "gemini", "claude", "copilot"]
    github_enabled: bool
    github_project_url: str = ""
    github_repository_url: str = ""
    repo_base_url: Literal["https://github.com", "ssh://git@github.com"] = (
        "https://github.com"
    )
    has_google_api_key: bool
    has_openai_api_key: bool
    has_anthropic_api_key: bool


class ProjectConfigUpdateRequest(BaseModel):
    config_dir: Path
    env_file_path: Path
    language: Literal["en", "ja"]
    description: str = ""
    llm_api_type: Literal["openai", "gemini", "anthropic"]
    cli_agent: Literal["codex", "gemini", "claude", "copilot"]
    github_enabled: bool
    repository_name: str = ""
    owner: str = ""
    project_id: str = ""
    github_project_url: str = ""
    repo_base_url: Literal["https://github.com", "ssh://git@github.com"] = (
        "https://github.com"
    )
    google_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None


class MemberResolveRequest(BaseModel):
    person_type: Literal["human", "machine_user", "github_apps", "proxy_agent"]
    identity: str = Field(min_length=1)


class MemberResolveResponse(BaseModel):
    person_id: str
    github_username: str
    github_user_id: int
    git_email: str
