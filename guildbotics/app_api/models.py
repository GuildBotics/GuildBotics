from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from guildbotics.editions.simple.setup_service import GitHubProjectInput, LaneMapInput


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


class ConfigStatus(BaseModel):
    cwd: Path
    env_file: Path
    env_file_exists: bool
    config_dir: Path
    project_file: Path
    project_file_exists: bool
    storage_dir: Path
    machine_state_dir: Path | None = None
    workspace_data_dir: Path | None = None


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
    source: Literal["workspace", "template"]
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
    trace_id: str
    output: str


class TraceSummary(BaseModel):
    trace_id: str
    source: str = ""
    person_id: str = ""
    command: str = ""
    workflow: str = ""
    started_at: str = ""
    updated_at: str = ""
    status: str = "info"
    event_count: int = 0
    log_count: int = 0
    error_count: int = 0
    span_count: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceRecord(BaseModel):
    kind: str
    timestamp: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    parent_id: str | None = None
    call_id: str | None = None
    span: str = ""
    source: str = ""
    person_id: str = ""
    command: str = ""
    workflow: str = ""
    type: str = ""
    level: str = ""
    message: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class TracesResponse(BaseModel):
    traces: list[TraceSummary] = Field(default_factory=list)


class TraceDetailResponse(BaseModel):
    trace_id: str
    summary: TraceSummary | None = None
    records: list[TraceRecord] = Field(default_factory=list)


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
    events_auth_failed_count: int | None = None
    events_auth_failed_persons: list[str] = Field(default_factory=list)


class RuntimeStatus(BaseModel):
    scheduler: RuntimeUnitStatus
    events: RuntimeUnitStatus


class PromptTraceUpdateRequest(BaseModel):
    enabled: bool
    trace_path: str = ""


class RuntimeDebugUpdateRequest(BaseModel):
    enabled: bool


class RuntimeDebugStatus(BaseModel):
    enabled: bool
    log_level: str
    agno_debug: bool
    env_file: Path
    env_file_exists: bool


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
    lane_map: LaneMapInput = Field(default_factory=LaneMapInput)
    has_google_api_key: bool
    has_openai_api_key: bool
    has_anthropic_api_key: bool


class ProjectStatusOptionsRequest(BaseModel):
    """Identify the GitHub Project to read Status options from.

    The form supplies the project being edited (which may not be saved yet);
    the backend reads its Status options live using a configured member's
    credentials, without writing anything to GitHub.
    """

    owner: str = ""
    project_id: str = ""
    github_project_url: str = ""


class ProjectStatusOptionsResponse(BaseModel):
    """Status (lane) option names read from a GitHub Project.

    ``available`` is False when options could not be read (incomplete project
    identity, no member credentials, or a GitHub error); the frontend then
    falls back to manual lane-name entry.
    """

    available: bool
    statuses: list[str] = Field(default_factory=list)


class AgentFieldOption(BaseModel):
    """A single ``Agent`` field option, keyed by a member's proxy signature."""

    name: str
    description: str = ""


class AgentFieldStateResponse(BaseModel):
    """State of a GitHub Project's ``Agent`` single-select field.

    ``available`` is False when the field state could not be read (incomplete
    project identity, no member credentials, or a GitHub error). ``options`` are
    the members currently registered as field options; ``missing`` are
    configured non-human members not yet registered (what an ensure call adds).
    """

    available: bool
    exists: bool = False
    options: list[AgentFieldOption] = Field(default_factory=list)
    missing: list[AgentFieldOption] = Field(default_factory=list)


class ProjectConfigUpdateRequest(GitHubProjectInput):
    config_dir: Path
    env_file_path: Path
    language: Literal["en", "ja"]
    description: str = ""
    llm_api_type: Literal["openai", "gemini", "anthropic"]
    cli_agent: Literal["codex", "gemini", "claude", "copilot"]
    github_enabled: bool
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
