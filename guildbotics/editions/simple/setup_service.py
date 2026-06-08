from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import requests  # type: ignore
from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from guildbotics.utils.fileio import get_template_path, load_yaml_file, save_yaml_file

BASE_DIR = Path(__file__).parent
TEMPLATE_PATH = BASE_DIR / "templates"
SAMPLE_COMMANDS_PATH = TEMPLATE_PATH / "sample_commands"
GITHUB_URL = "https://github.com/"
GITHUB_APPS_URL_MIN_PARTS = 8
GITHUB_PROJECT_URL_MIN_PARTS = 7
GITHUB_REPOSITORY_URL_MIN_PARTS = 5
CRON_FIELD_COUNT = 5
HTTP_OK = 200
GITHUB_USER_LOOKUP_TIMEOUT_SECONDS = 10.0
SLACK_CHANNEL_ID_PATTERN = re.compile(r"^[CGD][A-Z0-9]{8,}$")


def _to_int_or_none(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


class SetupServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GitHubProjectReference(BaseModel):
    project_type: str
    owner: str
    project_id: str
    url: str


class GitHubRepositoryReference(BaseModel):
    owner: str
    repository_name: str
    url: str


class GitHubUserReference(BaseModel):
    person_id: str
    github_username: str
    github_user_id: int
    git_email: str


class ProjectSetupInput(BaseModel):
    config_dir: Path
    env_file_path: Path
    env_file_option: str = Field(pattern="^(skip|append|overwrite)$")
    language: str
    description: str = ""
    repository_name: str = ""
    owner: str = ""
    project_id: str = ""
    github_project_url: str = ""
    repo_base_url: str = Field(
        default="https://github.com",
        pattern="^(https://github.com|ssh://git@github.com)$",
    )
    llm_api_type: str = Field(pattern="^(openai|gemini|anthropic)$")
    cli_agent: str = Field(pattern="^(codex|gemini|claude|copilot)$")
    google_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in {"en", "ja"}:
            raise ValueError("language must be one of: en, ja")
        return value

    @model_validator(mode="after")
    def validate_env_file_operation(self) -> ProjectSetupInput:
        if self.env_file_option == "append" and not self.env_file_path.exists():
            raise ValueError("env_file_option=append requires an existing env file")
        github_fields = [
            self.repository_name,
            self.owner,
            self.project_id,
            self.github_project_url,
        ]
        if any(github_fields) and not all(github_fields):
            raise ValueError(
                "repository_name, owner, project_id and github_project_url "
                "are required when GitHub integration is enabled"
            )
        return self


class CreatedFile(BaseModel):
    path: Path
    action: str


class ProjectSetupResult(BaseModel):
    files: list[CreatedFile]


class ProjectConfigSnapshot(BaseModel):
    config_dir: Path
    env_file_path: Path
    language: str
    description: str = ""
    llm_api_type: str = Field(pattern="^(openai|gemini|anthropic)$")
    cli_agent: str = Field(pattern="^(codex|gemini|claude|copilot)$")
    github_enabled: bool
    github_project_url: str = ""
    github_repository_url: str = ""
    repo_base_url: str = Field(
        default="https://github.com",
        pattern="^(https://github.com|ssh://git@github.com)$",
    )
    has_google_api_key: bool = False
    has_openai_api_key: bool = False
    has_anthropic_api_key: bool = False


class ProjectUpdateInput(BaseModel):
    config_dir: Path
    env_file_path: Path
    language: str
    description: str = ""
    llm_api_type: str = Field(pattern="^(openai|gemini|anthropic)$")
    cli_agent: str = Field(pattern="^(codex|gemini|claude|copilot)$")
    github_enabled: bool = False
    repository_name: str = ""
    owner: str = ""
    project_id: str = ""
    github_project_url: str = ""
    repo_base_url: str = Field(
        default="https://github.com",
        pattern="^(https://github.com|ssh://git@github.com)$",
    )
    google_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in {"en", "ja"}:
            raise ValueError("language must be one of: en, ja")
        return value

    @model_validator(mode="after")
    def validate_github_fields(self) -> ProjectUpdateInput:
        github_fields = [
            self.repository_name,
            self.owner,
            self.project_id,
            self.github_project_url,
        ]
        if self.github_enabled and not all(github_fields):
            raise ValueError(
                "repository_name, owner, project_id and github_project_url "
                "are required when GitHub integration is enabled"
            )
        return self


class PersonTaskScheduleInput(BaseModel):
    command: str
    schedules: list[str] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        command = value.strip()
        if not command:
            raise ValueError("command is required")
        return command

    @field_validator("schedules")
    @classmethod
    def validate_schedules(cls, value: list[str]) -> list[str]:
        schedules = [schedule.strip() for schedule in value if schedule.strip()]
        for schedule in schedules:
            if len(schedule.split()) != CRON_FIELD_COUNT:
                raise ValueError("schedule must be a five-field cron expression")
        return schedules


def _read_task_schedules(raw_schedules: object) -> list[PersonTaskScheduleInput]:
    if not isinstance(raw_schedules, list):
        return []
    schedules = []
    for schedule in raw_schedules:
        if not isinstance(schedule, dict):
            continue
        try:
            schedules.append(PersonTaskScheduleInput.model_validate(schedule))
        except ValidationError:
            continue
    return schedules


class PersonSetupInput(BaseModel):
    config_dir: Path
    env_file_path: Path
    append_env_file: bool = False
    person_type: str
    person_id: str
    person_name: str
    is_active: bool
    github_username: str
    git_email: str
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
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)

    @field_validator("person_id")
    @classmethod
    def validate_person_id(cls, value: str) -> str:
        import re

        if not re.match(r"^[a-z0-9_-]+$", value):
            raise ValueError(
                "person_id must contain only lowercase letters, digits, _ or -"
            )
        return value

    @field_validator("person_name")
    @classmethod
    def validate_person_name(cls, value: str) -> str:
        if not value:
            raise ValueError("person_name is required")
        return value

    @model_validator(mode="after")
    def validate_env_file_operation(self) -> PersonSetupInput:
        if self.append_env_file and not self.env_file_path.exists():
            raise ValueError("append_env_file requires an existing env file")
        return self


class PersonSetupResult(BaseModel):
    files: list[CreatedFile]
    masked_environment_variables: list[str]


class PersonConfigSnapshot(BaseModel):
    person_id: str
    person_name: str
    person_type: str
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
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)


class PersonUpdateInput(PersonSetupInput):
    original_person_id: str


class SimpleProjectSetupService:
    def read_project_config(
        self, *, config_dir: Path, env_file_path: Path
    ) -> ProjectConfigSnapshot:
        project_file = config_dir / "team/project.yml"
        if not project_file.exists():
            raise SetupServiceError(
                "project_not_found", "Project config was not found."
            )
        project_data = cast(dict, load_yaml_file(project_file))

        model_mapping = self._load_mapping(
            config_dir / "intelligences/model_mapping.yml",
            get_template_path() / "intelligences/model_mapping.yml",
        )
        cli_mapping = self._load_mapping(
            config_dir / "intelligences/cli_agent_mapping.yml",
            get_template_path() / "intelligences/cli_agent_mapping.yml",
        )

        services = project_data.get("services", {})
        ticket_manager = services.get("ticket_manager", {}) if services else {}
        repositories = project_data.get("repositories", [])
        repository_name = (
            repositories[0].get("name", "")
            if isinstance(repositories, list) and repositories
            else ""
        )
        owner = str(ticket_manager.get("owner", ""))
        project_id = str(ticket_manager.get("project_id", ""))
        github_project_url = str(ticket_manager.get("url", ""))
        github_enabled = bool(
            owner and project_id and github_project_url and repository_name
        )

        code_hosting = services.get("code_hosting_service", {}) if services else {}
        repo_base_url = str(code_hosting.get("repo_base_url", "https://github.com"))

        env_values = (
            dict(dotenv_values(env_file_path)) if env_file_path.exists() else {}
        )
        return ProjectConfigSnapshot(
            config_dir=config_dir,
            env_file_path=env_file_path,
            language=str(project_data.get("language", "en")),
            description=str(project_data.get("description", "")),
            llm_api_type=self._infer_llm_api_type(model_mapping),
            cli_agent=self._infer_cli_agent(cli_mapping),
            github_enabled=github_enabled,
            github_project_url=github_project_url,
            github_repository_url=(
                f"https://github.com/{owner}/{repository_name}"
                if github_enabled
                else ""
            ),
            repo_base_url=repo_base_url,
            has_google_api_key=bool(env_values.get("GOOGLE_API_KEY")),
            has_openai_api_key=bool(env_values.get("OPENAI_API_KEY")),
            has_anthropic_api_key=bool(env_values.get("ANTHROPIC_API_KEY")),
        )

    def parse_github_project_url(self, url: str) -> GitHubProjectReference:
        url_parts = url.split("/")
        if (
            len(url_parts) < GITHUB_PROJECT_URL_MIN_PARTS
            or not url.startswith(GITHUB_URL)
            or url_parts[3] not in ["orgs", "users"]
            or url_parts[5] != "projects"
            or url_parts[6] == ""
        ):
            raise SetupServiceError(
                "invalid_github_project_url", "Invalid GitHub project URL."
            )

        project_type = url_parts[3]
        owner = url_parts[4]
        project_id = url_parts[6].split("?")[0]
        return GitHubProjectReference(
            project_type=project_type,
            owner=owner,
            project_id=project_id,
            url=f"{GITHUB_URL}{project_type}/{owner}/projects/{project_id}",
        )

    def parse_github_repository_url(
        self, url: str, *, owner: str
    ) -> GitHubRepositoryReference:
        url_parts = url.split("/")
        if (
            len(url_parts) < GITHUB_REPOSITORY_URL_MIN_PARTS
            or not url.startswith(GITHUB_URL)
            or url_parts[4] == ""
        ):
            raise SetupServiceError(
                "invalid_github_repository_url", "Invalid GitHub repository URL."
            )
        if url_parts[3] != owner:
            raise SetupServiceError(
                "inconsistent_github_url", "GitHub repository owner is inconsistent."
            )

        repository_name = url_parts[4]
        return GitHubRepositoryReference(
            owner=owner,
            repository_name=repository_name,
            url=f"{GITHUB_URL}{owner}/{repository_name}",
        )

    def write_project(self, config: ProjectSetupInput) -> ProjectSetupResult:
        files: list[CreatedFile] = []

        project_config_file = config.config_dir / "team/project.yml"
        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(project_config_file, self.build_project_config(config))
        files.append(CreatedFile(path=project_config_file, action="create"))

        model_mapping_template = get_template_path() / "intelligences/model_mapping.yml"
        model_mapping: dict = cast(dict, load_yaml_file(model_mapping_template))
        model_mapping["default"] = model_mapping[config.llm_api_type]
        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="create"))

        cli_mapping_template = (
            get_template_path() / "intelligences/cli_agent_mapping.yml"
        )
        cli_mapping: dict = cast(dict, load_yaml_file(cli_mapping_template))
        cli_mapping["default"] = cli_mapping[config.cli_agent]
        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        save_yaml_file(cli_mapping_file, cli_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="create"))

        cli_agent_config_src_dir = get_template_path() / "intelligences/cli_agents"
        cli_agent_config_dst_dir = config.config_dir / "intelligences/cli_agents"
        cli_agent_config_dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in cli_agent_config_src_dir.glob("*.yml"):
            dst_file = cli_agent_config_dst_dir / src_file.name
            dst_file.write_text(src_file.read_text())
            files.append(CreatedFile(path=dst_file, action="create"))

        files.extend(self.ensure_sample_commands(config.config_dir, config.language))

        if config.env_file_option != "skip":
            env_file = self.render_env_file(config)
            config.env_file_path.parent.mkdir(parents=True, exist_ok=True)
            if config.env_file_option == "overwrite":
                config.env_file_path.write_text(env_file)
                files.append(CreatedFile(path=config.env_file_path, action="create"))
            elif config.env_file_option == "append":
                config.env_file_path.write_text(
                    f"{config.env_file_path.read_text()}\n\n{env_file}"
                )
                files.append(CreatedFile(path=config.env_file_path, action="append"))

        return ProjectSetupResult(files=files)

    def ensure_sample_commands(
        self, config_dir: Path, language: str
    ) -> list[CreatedFile]:
        commands_dir = config_dir / "commands"
        if commands_dir.exists() and any(
            path.is_file() for path in commands_dir.rglob("*")
        ):
            return []

        sample_dir = SAMPLE_COMMANDS_PATH / language
        if not sample_dir.exists():
            sample_dir = SAMPLE_COMMANDS_PATH / "en"
        if not sample_dir.exists():
            return []

        files: list[CreatedFile] = []
        for src_file in sample_dir.rglob("*"):
            if not src_file.is_file():
                continue
            dst_file = commands_dir / src_file.relative_to(sample_dir)
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            if dst_file.suffix == ".sh":
                dst_file.chmod(0o755)
            files.append(CreatedFile(path=dst_file, action="create"))
        return files

    def update_project(self, config: ProjectUpdateInput) -> ProjectSetupResult:
        files: list[CreatedFile] = []
        project_config_file = config.config_dir / "team/project.yml"
        if project_config_file.exists():
            project_data = cast(dict, load_yaml_file(project_config_file))
        else:
            project_data = {}
        project_data["language"] = config.language
        if config.description:
            project_data["description"] = config.description
        else:
            project_data.pop("description", None)

        services = project_data.get("services", {})
        if not isinstance(services, dict):
            services = {}

        if config.github_enabled:
            services["ticket_manager"] = {
                "name": "GitHub",
                "owner": config.owner,
                "project_id": str(config.project_id),
                "url": config.github_project_url,
                "lane_map": {
                    "ready": "Todo",
                    "working": "In Progress",
                    "done": "Done",
                },
            }
            services["code_hosting_service"] = {
                "name": "GitHub",
                "owner": config.owner,
                "repo_base_url": config.repo_base_url,
            }
            project_data["repositories"] = [{"name": config.repository_name}]
        else:
            services.pop("ticket_manager", None)
            services.pop("code_hosting_service", None)

        if services:
            project_data["services"] = services
        else:
            project_data.pop("services", None)

        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(project_config_file, project_data)
        files.append(CreatedFile(path=project_config_file, action="update"))

        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        model_mapping = self._load_mapping(
            model_mapping_file,
            get_template_path() / "intelligences/model_mapping.yml",
        )
        model_mapping["default"] = model_mapping[config.llm_api_type]
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="update"))

        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        cli_mapping = self._load_mapping(
            cli_mapping_file,
            get_template_path() / "intelligences/cli_agent_mapping.yml",
        )
        cli_mapping["default"] = cli_mapping[config.cli_agent]
        cli_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(cli_mapping_file, cli_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        env_updates = {
            "GOOGLE_API_KEY": config.google_api_key,
            "OPENAI_API_KEY": config.openai_api_key,
            "ANTHROPIC_API_KEY": config.anthropic_api_key,
        }
        if any(value for value in env_updates.values() if value):
            env_file_existed = config.env_file_path.exists()
            env_values = (
                dict(dotenv_values(config.env_file_path)) if env_file_existed else {}
            )
            for key, value in env_updates.items():
                if value:
                    env_values[key] = value
            lines = [
                f"{key}={value}"
                for key, value in env_values.items()
                if value is not None
            ]
            config.env_file_path.parent.mkdir(parents=True, exist_ok=True)
            config.env_file_path.write_text("\n".join(lines))
            files.append(
                CreatedFile(
                    path=config.env_file_path,
                    action="update" if env_file_existed else "create",
                )
            )

        files.extend(self.ensure_sample_commands(config.config_dir, config.language))

        return ProjectSetupResult(files=files)

    def build_project_config(self, config: ProjectSetupInput) -> dict:
        project: dict = {"language": config.language}
        if config.description:
            project["description"] = config.description

        if config.repository_name:
            project["repositories"] = [{"name": config.repository_name}]
            project["services"] = {
                "ticket_manager": {
                    "name": "GitHub",
                    "owner": config.owner,
                    "project_id": str(config.project_id),
                    "url": config.github_project_url,
                    "lane_map": {
                        "ready": "Todo",
                        "working": "In Progress",
                        "done": "Done",
                    },
                },
                "code_hosting_service": {
                    "name": "GitHub",
                    "owner": config.owner,
                    "repo_base_url": config.repo_base_url,
                },
            }
        return project

    def render_env_file(self, config: ProjectSetupInput) -> str:
        env_file_template = (TEMPLATE_PATH / ".env.example").read_text()
        return env_file_template.format(
            google_api_key=f"GOOGLE_API_KEY={config.google_api_key}",
            openai_api_key=f"OPENAI_API_KEY={config.openai_api_key}",
            anthropic_api_key=f"ANTHROPIC_API_KEY={config.anthropic_api_key}",
        )

    def _load_mapping(self, file_path: Path, template_path: Path) -> dict:
        if file_path.exists():
            return cast(dict, load_yaml_file(file_path))
        return cast(dict, load_yaml_file(template_path))

    def _infer_llm_api_type(self, mapping: dict) -> str:
        default = str(mapping.get("default", ""))
        for provider in ("openai", "gemini", "anthropic"):
            if default == str(mapping.get(provider, "")):
                return provider
        if "openai" in default:
            return "openai"
        if "anthropic" in default or "claude" in default:
            return "anthropic"
        return "gemini"

    def _infer_cli_agent(self, mapping: dict) -> str:
        default = str(mapping.get("default", ""))
        for agent in ("codex", "gemini", "claude", "copilot"):
            if default == str(mapping.get(agent, "")):
                return agent
        if "codex" in default:
            return "codex"
        if "claude" in default:
            return "claude"
        if "copilot" in default:
            return "copilot"
        return "gemini"


class SimplePersonSetupService:
    def read_person_config(
        self, *, config_dir: Path, person_id: str, env_file_path: Path
    ) -> PersonConfigSnapshot:
        person_file = config_dir / f"team/members/{person_id}/person.yml"
        if not person_file.exists():
            raise SetupServiceError("person_not_found", "Member config was not found.")
        person_data = cast(dict, load_yaml_file(person_file))
        account_info = person_data.get("account_info", {})
        profile = person_data.get("profile", {})
        professional = (
            profile.get("professional", {}) if isinstance(profile, dict) else {}
        )
        character = profile.get("character", {}) if isinstance(profile, dict) else {}
        roles = list(professional.keys()) if isinstance(professional, dict) else []
        character_data = character if isinstance(character, dict) else {}

        channels: list[str] = []
        raw_channels = person_data.get("message_channels", [])
        if isinstance(raw_channels, list):
            channels = [
                str(channel.get("name", ""))
                for channel in raw_channels
                if isinstance(channel, dict)
                and str(channel.get("service", "")).lower() == "slack"
            ]
            channels = [channel for channel in channels if channel]

        env = self._read_env_values(env_file_path)
        env_prefix = self._person_env_prefix(person_id)
        return PersonConfigSnapshot(
            person_id=str(person_data.get("person_id", person_id)),
            person_name=str(person_data.get("name", "")),
            person_type=str(person_data.get("person_type", "")),
            is_active=bool(person_data.get("is_active", False)),
            github_username=str(account_info.get("github_username", "")),
            git_email=str(account_info.get("git_email", "")),
            roles=roles,
            speaking_style=str(person_data.get("speaking_style", "")),
            relationships=str(person_data.get("relationships", "")),
            character=character_data,
            github_installation_id=_to_int_or_none(
                env.get(f"{env_prefix}_GITHUB_INSTALLATION_ID")
            ),
            github_app_id=_to_int_or_none(env.get(f"{env_prefix}_GITHUB_APP_ID")),
            github_private_key_path=str(
                env.get(f"{env_prefix}_GITHUB_PRIVATE_KEY_PATH") or ""
            ),
            has_github_installation_id=bool(
                env.get(f"{env_prefix}_GITHUB_INSTALLATION_ID")
            ),
            has_github_app_id=bool(env.get(f"{env_prefix}_GITHUB_APP_ID")),
            has_github_private_key_path=bool(
                env.get(f"{env_prefix}_GITHUB_PRIVATE_KEY_PATH")
            ),
            has_github_access_token=bool(env.get(f"{env_prefix}_GITHUB_ACCESS_TOKEN")),
            has_slack_bot_token=bool(env.get(f"{env_prefix}_SLACK_BOT_TOKEN")),
            has_slack_app_token=bool(env.get(f"{env_prefix}_SLACK_APP_TOKEN")),
            slack_channels=channels,
            routine_commands=[
                str(command).strip()
                for command in person_data.get("routine_commands", [])
                if str(command).strip()
            ],
            task_schedules=_read_task_schedules(person_data.get("task_schedules", [])),
        )

    def parse_github_apps_url(self, url: str) -> str:
        url_parts = url.split("/")
        if (
            len(url_parts) < GITHUB_APPS_URL_MIN_PARTS
            or not url.startswith(GITHUB_URL)
            or url_parts[3] != "organizations"
            or url_parts[5] != "settings"
            or url_parts[6] != "apps"
            or url_parts[7] == ""
        ):
            raise SetupServiceError(
                "invalid_github_apps_url", "Invalid GitHub Apps URL."
            )
        return url_parts[7]

    def resolve_github_user(
        self, name: str, *, is_github_apps: bool = False
    ) -> GitHubUserReference:
        if name == "":
            raise SetupServiceError(
                "invalid_github_username", "Invalid GitHub username."
            )

        if is_github_apps:
            person_id = name
            github_username = f"{name}[bot]"
            api_username = quote(github_username)
        else:
            person_id = name.split("@", maxsplit=1)[0]
            github_username = person_id
            api_username = person_id

        try:
            response = requests.get(
                f"https://api.github.com/users/{api_username}",
                timeout=GITHUB_USER_LOOKUP_TIMEOUT_SECONDS,
            )
            if response.status_code != HTTP_OK:
                raise SetupServiceError(
                    "invalid_github_username", "Invalid GitHub username."
                )
            data = response.json()
            github_user_id = data.get("id", 0)
        except SetupServiceError:
            raise
        except Exception as exc:
            raise SetupServiceError(
                "invalid_github_username", "Invalid GitHub username."
            ) from exc

        return GitHubUserReference(
            person_id=person_id,
            github_username=github_username,
            github_user_id=github_user_id,
            git_email=f"{github_user_id}+{github_username}@users.noreply.github.com",
        )

    def write_person(self, config: PersonSetupInput) -> PersonSetupResult:
        files: list[CreatedFile] = []
        env_vars = self.build_environment_variables(config)
        person_config_file = (
            config.config_dir / f"team/members/{config.person_id}/person.yml"
        )
        person_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(person_config_file, self.build_person_config(config))
        files.append(CreatedFile(path=person_config_file, action="create"))

        if config.append_env_file and env_vars:
            config.env_file_path.write_text(
                f"{config.env_file_path.read_text()}\n\n# {config.person_id}\n"
                + "\n".join(env_vars)
            )
            files.append(CreatedFile(path=config.env_file_path, action="append"))

        return PersonSetupResult(
            files=files,
            masked_environment_variables=[
                self.mask_env_var(env_var) for env_var in env_vars
            ],
        )

    def update_person(self, config: PersonUpdateInput) -> PersonSetupResult:
        files: list[CreatedFile] = []
        original_person_file = (
            config.config_dir / f"team/members/{config.original_person_id}/person.yml"
        )
        if not original_person_file.exists():
            raise SetupServiceError("person_not_found", "Member config was not found.")

        old_person_dir = original_person_file.parent
        new_person_dir = config.config_dir / f"team/members/{config.person_id}"
        if old_person_dir != new_person_dir and new_person_dir.exists():
            raise SetupServiceError("person_id_conflict", "Member ID already exists.")
        if old_person_dir != new_person_dir:
            new_person_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_person_dir), str(new_person_dir))

        person_file = new_person_dir / "person.yml"
        person_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(person_file, self.build_person_config(config))
        files.append(CreatedFile(path=person_file, action="update"))

        env_file_existed = config.env_file_path.exists()
        env_values = self._read_env_values(config.env_file_path)
        preserved_env_values = self._renamed_existing_person_env_values(
            env_values,
            original_person_id=config.original_person_id,
            person_id=config.person_id,
        )
        for key in self._managed_person_env_keys(config.original_person_id):
            env_values.pop(key, None)
        env_values.update(preserved_env_values)
        for line in self.build_environment_variables(config):
            key, _, value = line.partition("=")
            env_values[key] = value
        self._write_env_values(config.env_file_path, env_values)
        files.append(
            CreatedFile(
                path=config.env_file_path,
                action="update" if env_file_existed else "create",
            )
        )

        return PersonSetupResult(
            files=files,
            masked_environment_variables=[
                self.mask_env_var(env_var)
                for env_var in self.build_environment_variables(config)
            ],
        )

    def delete_person(
        self, *, config_dir: Path, person_id: str, env_file_path: Path
    ) -> PersonSetupResult:
        files: list[CreatedFile] = []
        person_dir = config_dir / f"team/members/{person_id}"
        person_file = person_dir / "person.yml"
        if not person_file.exists():
            raise SetupServiceError("person_not_found", "Member config was not found.")

        shutil.rmtree(person_dir)
        files.append(CreatedFile(path=person_file, action="delete"))

        env_values = self._read_env_values(env_file_path)
        removed = False
        for key in self._managed_person_env_keys(person_id):
            if key in env_values:
                removed = True
                env_values.pop(key, None)
        if removed or env_file_path.exists():
            self._write_env_values(env_file_path, env_values)
            files.append(CreatedFile(path=env_file_path, action="update"))
        return PersonSetupResult(files=files, masked_environment_variables=[])

    def build_person_config(self, config: PersonSetupInput) -> dict:
        professional_roles: dict[str, dict[str, str]] = {
            role.rstrip(":"): {} for role in config.roles
        }
        profile: dict[str, Any] = {
            "professional": professional_roles,
            "personal": {},
            "programmer": {},
        }
        if config.character:
            profile["character"] = config.character
        person_config = {
            "person_id": config.person_id,
            "name": config.person_name,
            "is_active": config.is_active,
            "person_type": config.person_type,
            "account_info": {
                "github_username": config.github_username,
                "git_user": config.person_name,
                "git_email": config.git_email,
            },
            "profile": profile,
            "speaking_style": config.speaking_style,
            "relationships": config.relationships,
        }
        if config.slack_channels:
            person_config["message_channels"] = [
                self._build_slack_message_channel(channel, person_id=config.person_id)
                for channel in config.slack_channels
                if channel
            ]
        routine_commands = [
            command.strip() for command in config.routine_commands if command.strip()
        ]
        if routine_commands:
            person_config["routine_commands"] = routine_commands
        task_schedules = [
            schedule.model_dump()
            for schedule in config.task_schedules
            if schedule.command.strip() and schedule.schedules
        ]
        if task_schedules:
            person_config["task_schedules"] = task_schedules
        return person_config

    def _build_slack_message_channel(
        self, channel: str, *, person_id: str
    ) -> dict[str, Any]:
        channel_ref = channel.strip().lstrip("#")
        chat: dict[str, Any] = {"enabled": True}
        channel_info: dict[str, Any] = {}
        if SLACK_CHANNEL_ID_PATTERN.fullmatch(channel_ref):
            chat["channel_id"] = channel_ref
            channel_info["channel_id"] = channel_ref
        else:
            chat["channel_name"] = channel_ref
        return {
            "name": channel_ref,
            "service": "slack",
            "used_as": ["internal_communication"],
            "used_by": [person_id],
            "channel_info": channel_info,
            "chat": chat,
        }

    def build_environment_variables(self, config: PersonSetupInput) -> list[str]:
        sanitized_id = config.person_id.replace("-", "_").upper()
        env_vars: list[str] = []

        if config.github_installation_id is not None:
            env_vars.append(
                f"{sanitized_id}_GITHUB_INSTALLATION_ID={config.github_installation_id}"
            )
        if config.github_app_id is not None:
            env_vars.append(f"{sanitized_id}_GITHUB_APP_ID={config.github_app_id}")
        if config.github_private_key_path is not None:
            env_vars.append(
                f"{sanitized_id}_GITHUB_PRIVATE_KEY_PATH={config.github_private_key_path}"
            )
        if config.github_access_token:
            env_vars.append(
                f"{sanitized_id}_GITHUB_ACCESS_TOKEN={config.github_access_token}"
            )
        if config.slack_bot_token:
            env_vars.append(f"{sanitized_id}_SLACK_BOT_TOKEN={config.slack_bot_token}")
        if config.slack_app_token:
            env_vars.append(f"{sanitized_id}_SLACK_APP_TOKEN={config.slack_app_token}")

        return env_vars

    def mask_env_var(self, env_var: str) -> str:
        key, sep, value = env_var.partition("=")
        if not sep:
            return env_var
        if key.endswith("_PATH") or key.endswith("_ID"):
            return env_var
        if not value:
            return env_var
        return f"{key}=********"

    def _person_env_prefix(self, person_id: str) -> str:
        return person_id.replace("-", "_").upper()

    def _managed_person_env_keys(self, person_id: str) -> list[str]:
        prefix = self._person_env_prefix(person_id)
        return [
            f"{prefix}_GITHUB_INSTALLATION_ID",
            f"{prefix}_GITHUB_APP_ID",
            f"{prefix}_GITHUB_PRIVATE_KEY_PATH",
            f"{prefix}_GITHUB_ACCESS_TOKEN",
            f"{prefix}_SLACK_BOT_TOKEN",
            f"{prefix}_SLACK_APP_TOKEN",
        ]

    def _renamed_existing_person_env_values(
        self,
        env_values: dict[str, str],
        *,
        original_person_id: str,
        person_id: str,
    ) -> dict[str, str]:
        old_prefix = self._person_env_prefix(original_person_id)
        new_prefix = self._person_env_prefix(person_id)
        preserved: dict[str, str] = {}
        for old_key in self._managed_person_env_keys(original_person_id):
            if old_key not in env_values:
                continue
            suffix = old_key.removeprefix(f"{old_prefix}_")
            preserved[f"{new_prefix}_{suffix}"] = env_values[old_key]
        return preserved

    def _read_env_values(self, env_file_path: Path) -> dict[str, str]:
        raw_values = (
            dict(dotenv_values(env_file_path)) if env_file_path.exists() else {}
        )
        return {
            str(key): str(value)
            for key, value in raw_values.items()
            if value is not None
        }

    def _write_env_values(
        self, env_file_path: Path, env_values: dict[str, str]
    ) -> None:
        env_file_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{key}={value}" for key, value in env_values.items()]
        env_file_path.write_text("\n".join(lines))
