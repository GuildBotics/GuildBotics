from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guildbotics.app_api.activity_events import ActivityEventType
from guildbotics.editions.simple.setup_service import GitHubProjectInput, LaneMapInput
from guildbotics.intelligences.llm_providers import LlmProviderInfo


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
    person_type: str = ""
    is_active: bool
    roles: list[str]


class TeamSummary(BaseModel):
    project: ProjectSummary
    members: list[MemberSummary]


class MemberDeleteRequest(BaseModel):
    config_dir: Path
    env_file_path: Path


class CommandArgumentOption(BaseModel):
    name: str
    kind: Literal["positional", "keyword"]
    required: bool = False
    default: str = ""


class CommandRequirement(BaseModel):
    kind: Literal["github", "slack", "cli_agent", "llm"]
    satisfied: bool
    message: str = ""


class CommandInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defined_args: Literal["auto", "hidden"] = "auto"
    extra_args: Literal["hidden", "optional"] = "hidden"
    message: Literal["hidden", "optional", "required"] = "optional"


class CommandOption(BaseModel):
    command: str
    label: str
    description: str = ""
    category: Literal["workflow", "function", "example", "custom"]
    source: Literal["workspace", "template"]
    path: Path
    arguments: list[CommandArgumentOption] = Field(default_factory=list)
    inputs: CommandInputs = Field(default_factory=CommandInputs)
    requirements: list[CommandRequirement] = Field(default_factory=list)
    # Routine candidates that still require caller-supplied input cannot run on a
    # schedule; they stay listed but are flagged ineligible so the UI can explain.
    routine_eligible: bool = True


class CommandOptionsResponse(BaseModel):
    options: list[CommandOption]


class RoutineCommandOptionsResponse(BaseModel):
    options: list[CommandOption]
    # Command to seed / pre-select for a new member. Empty when no candidate.
    default_command: str = ""


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


class TracePresentation(BaseModel):
    """Provider-neutral display contract for one diagnostics record."""

    label_key: str = ""
    label_fallback: str = ""
    message_key: str = ""
    message: str = ""
    message_params: dict[str, Any] = Field(default_factory=dict)
    tone: str = "neutral"


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
    presentation: TracePresentation = Field(default_factory=TracePresentation)


class TracesResponse(BaseModel):
    traces: list[TraceSummary] = Field(default_factory=list)


class TraceDetailResponse(BaseModel):
    trace_id: str
    summary: TraceSummary | None = None
    records: list[TraceRecord] = Field(default_factory=list)
    transcript_available: bool = True


class MemoryEvent(BaseModel):
    timestamp: str = ""
    action: str = ""
    person_id: str = ""
    scope: str = ""
    doc_id: str = ""
    path: str = ""
    title: str = ""
    summary: str = ""
    kind: str = ""
    trace_id: str | None = None
    run_id: str = ""
    task_run_id: str = ""
    source: list[dict[str, Any]] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    query_keywords: list[str] = Field(default_factory=list)
    result_count: int | None = None
    duration_ms: float | None = None
    body_preview: str = ""


class MemoryEventsResponse(BaseModel):
    event_count: int = 0
    events: list[MemoryEvent] = Field(default_factory=list)


class ActivityHistoryMember(BaseModel):
    person_id: str
    name: str
    person_type: str = ""
    roles: list[str] = Field(default_factory=list)


class ActivityHistoryLink(BaseModel):
    kind: Literal["doc", "issue", "pull_request", "commit", "external"]
    label: str
    url: str = ""
    timestamp: str = ""


class ActivityHistoryRateLimit(BaseModel):
    retry_after_at: str = ""
    retry_after_text: str = ""


class ActivityHistorySession(BaseModel):
    trace_id: str
    person_id: str
    source: str = ""
    command: str = ""
    workflow: str = ""
    title: str = ""
    mode: Literal["interactive", "workflow"] = "workflow"
    status: str = "info"
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0
    links: list[ActivityHistoryLink] = Field(default_factory=list)
    rate_limit: ActivityHistoryRateLimit | None = None


class ActivityHistoryEvent(BaseModel):
    id: str
    timestamp: str
    person_id: str = ""
    type: ActivityEventType
    title: str
    detail: str = ""
    url: str = ""
    links: list[ActivityHistoryLink] = Field(default_factory=list)


class ActivityHistoryResponse(BaseModel):
    start: str
    end: str
    members: list[ActivityHistoryMember] = Field(default_factory=list)
    sessions: list[ActivityHistorySession] = Field(default_factory=list)
    events: list[ActivityHistoryEvent] = Field(default_factory=list)
    unsupported_event_sources: list[str] = Field(default_factory=list)


class RuntimeSourceSelection(BaseModel):
    scheduled: bool = True
    routine: bool = True
    event_queue: bool = True

    @model_validator(mode="after")
    def require_one_enabled(self) -> RuntimeSourceSelection:
        if not (self.scheduled or self.routine or self.event_queue):
            raise ValueError("at least one runtime source must be enabled")
        return self


class SchedulerStartRequest(BaseModel):
    sources: RuntimeSourceSelection = Field(default_factory=RuntimeSourceSelection)
    max_consecutive_errors: int = Field(default=3, ge=1)
    routine_interval_minutes: int = Field(default=10, ge=1)


class SchedulerStopRequest(BaseModel):
    force: bool = False


class RuntimeActiveWork(BaseModel):
    id: str
    source: Literal["manual", "scheduled", "routine", "event_queue"]
    person_id: str
    command: str
    started_at: str


class RuntimeUnitStatus(BaseModel):
    target: Literal["scheduler", "events"]
    state: Literal["starting", "running", "stopping", "stopped", "failed"]
    running: bool
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None
    max_consecutive_errors: int | None = None
    routine_interval_minutes: int | None = None
    active_member_count: int | None = None
    worker_count: int | None = None
    scheduled_source_enabled: bool | None = None
    routine_source_enabled: bool | None = None
    event_queue_source_enabled: bool | None = None
    subscription_count: int | None = None
    listener_count: int | None = None
    cycle_count: int | None = None
    cycle_failure_count: int | None = None
    events_drained_count: int | None = None
    events_auth_failed_count: int | None = None
    events_auth_failed_persons: list[str] = Field(default_factory=list)


class RuntimeStatus(BaseModel):
    scheduler: RuntimeUnitStatus
    events: RuntimeUnitStatus
    active_works: list[RuntimeActiveWork] = Field(default_factory=list)


class ChatReceiveResetResponse(BaseModel):
    members_reset: int
    channels_reset: int


class RuntimeDebugUpdateRequest(BaseModel):
    enabled: bool


class RuntimeDebugStatus(BaseModel):
    enabled: bool
    log_level: str
    agno_debug: bool
    env_file: Path
    env_file_exists: bool


class TranscriptSettingsUpdateRequest(BaseModel):
    detail: Literal["standard", "full"]
    retention_days: int = Field(ge=1, le=3650)


class TranscriptSettingsStatus(BaseModel):
    detail: Literal["standard", "full"]
    retention_days: int
    env_file: Path
    env_file_exists: bool
    sessions_dir: Path
    total_size_bytes: int
    index_size_bytes: int
    index_rewrite_threshold_bytes: int
    memory_size_bytes: int
    memory_max_size_bytes: int


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


SystemAlertCode = Literal[
    "credential_github",
    "credential_slack",
    "credential_cli_agent",
    "credential_llm",
    "command_failed",
    "rate_limited",
    "scheduler_failed",
    "worker_stopped",
]
SystemAlertSeverity = Literal["critical", "warning"]
SystemAlertAction = Literal["diagnostics", "setup", "trace", "service"]


class SystemAlert(BaseModel):
    id: str
    code: SystemAlertCode
    severity: SystemAlertSeverity
    opened_at: str
    updated_at: str
    occurrence_count: int = 1
    person_id: str = ""
    command: str = ""
    trace_id: str = ""
    actions: list[SystemAlertAction] = Field(default_factory=list)


class SystemAlertsResponse(BaseModel):
    alerts: list[SystemAlert] = Field(default_factory=list)


class SystemAlertDismissRequest(BaseModel):
    alert_id: str = Field(min_length=1)


class CliAgentDetection(BaseModel):
    name: str
    label: str = ""
    executable: str
    config_reference: str
    detected: bool
    path: str = ""


class CliAgentDetectionsResponse(BaseModel):
    agents: list[CliAgentDetection]


class CliAgentUsageWindow(BaseModel):
    window: str
    used_percent: float
    resets_at: str = ""
    window_minutes: int | None = None


class CliAgentUsage(BaseModel):
    agent: str
    windows: list[CliAgentUsageWindow] = Field(default_factory=list)
    limit_reached: bool = False
    checked_at: str = ""


class CliAgentUsagesResponse(BaseModel):
    usages: list[CliAgentUsage] = Field(default_factory=list)


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


class CodexNativeAgentPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    filesystem_access: Literal["workspace", "host"] = "workspace"


class NativeAgentPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    codex: CodexNativeAgentPolicySettings = Field(
        default_factory=CodexNativeAgentPolicySettings
    )


class LlmProvidersResponse(BaseModel):
    providers: list[LlmProviderInfo] = Field(default_factory=list)


class IntelligenceConfigResponse(BaseModel):
    config_dir: Path
    person_id: str | None = None
    inherited: bool = False
    model_mapping: dict[str, str] = Field(default_factory=dict)
    models: list[ModelDefinition] = Field(default_factory=list)
    cli_agent_mapping: dict[str, str] = Field(default_factory=dict)
    cli_agents: list[CliAgentDefinition] = Field(default_factory=list)
    brain_mapping: list[BrainAssignment] = Field(default_factory=list)
    native_agent_policy: NativeAgentPolicySettings = Field(
        default_factory=NativeAgentPolicySettings
    )


class IntelligenceConfigUpdateRequest(BaseModel):
    config_dir: Path
    person_id: str | None = None
    inherit_team_defaults: bool = False
    model_mapping: dict[str, str] = Field(default_factory=dict)
    models: list[ModelDefinition] = Field(default_factory=list)
    cli_agent_mapping: dict[str, str] = Field(default_factory=dict)
    cli_agents: list[CliAgentDefinition] = Field(default_factory=list)
    brain_mapping: list[BrainAssignment] = Field(default_factory=list)
    native_agent_policy: NativeAgentPolicySettings | None = None


class ProjectConfigResponse(BaseModel):
    config_dir: Path
    env_file_path: Path
    language: str
    description: str = ""
    llm_api_type: str
    cli_agent: str
    github_enabled: bool
    github_project_url: str = ""
    lane_map: LaneMapInput = Field(default_factory=LaneMapInput)
    # provider id -> whether its API key is configured in the .env
    provider_api_keys: dict[str, bool] = Field(default_factory=dict)


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
    llm_api_type: str
    cli_agent: str
    github_enabled: bool
    # provider id -> new API key value to write to the .env (empty/absent = leave as is)
    provider_api_keys: dict[str, str] = Field(default_factory=dict)


class MemberResolveRequest(BaseModel):
    person_type: Literal["human", "machine_user", "github_apps", "proxy_agent"]
    identity: str = Field(min_length=1)


class MemberResolveResponse(BaseModel):
    person_id: str
    github_username: str
    github_user_id: int
    git_email: str
