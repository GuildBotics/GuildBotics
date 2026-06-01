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


class ConfigStatus(BaseModel):
    cwd: Path
    env_file: Path
    env_file_exists: bool
    primary_config_dir: Path
    primary_project_file: Path
    primary_project_file_exists: bool
    home_config_dir: Path
    home_project_file: Path
    home_project_file_exists: bool
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


class MemberConfigResponse(BaseModel):
    person_id: str
    person_name: str
    person_type: Literal["", "human", "machine_user", "github_apps", "proxy_agent"]
    is_active: bool
    github_username: str
    git_email: str
    roles: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    relationships: str = ""
    character: dict[str, Any] = Field(default_factory=dict)
    github_installation_id: int | None = None
    github_app_id: int | None = None
    github_private_key_path: str = ""
    has_github_installation_id: bool = False
    has_github_app_id: bool = False
    has_github_private_key_path: bool = False
    has_github_access_token: bool = False
    has_slack_bot_token: bool = False
    has_slack_app_token: bool = False
    slack_channels: list[str] = Field(default_factory=list)


class MemberConfigUpdateRequest(BaseModel):
    config_dir: Path
    env_file_path: Path
    original_person_id: str
    person_type: Literal["", "human", "machine_user", "github_apps", "proxy_agent"]
    person_id: str = Field(min_length=1)
    person_name: str = Field(min_length=1)
    is_active: bool
    github_username: str = ""
    git_email: str = ""
    roles: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    relationships: str = ""
    character: dict[str, Any] = Field(default_factory=dict)
    github_installation_id: int | None = None
    github_app_id: int | None = None
    github_private_key_path: Path | None = None
    github_access_token: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channels: list[str] = Field(default_factory=list)


class MemberDeleteRequest(BaseModel):
    config_dir: Path
    env_file_path: Path


class RoutineOption(BaseModel):
    command: str
    requires_github: bool


class RoutineOptionsResponse(BaseModel):
    routines: list[RoutineOption]


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


class RuntimeUnitStatus(BaseModel):
    target: Literal["scheduler", "events"]
    state: Literal["starting", "running", "stopping", "stopped", "failed"]
    running: bool
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None


class RuntimeStatus(BaseModel):
    scheduler: RuntimeUnitStatus
    events: RuntimeUnitStatus


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
