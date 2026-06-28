from __future__ import annotations

import contextlib
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
CRON_FIELD_COUNT = 5
HTTP_OK = 200
GITHUB_USER_LOOKUP_TIMEOUT_SECONDS = 10.0
SLACK_CHANNEL_ID_PATTERN = re.compile(r"^[CGD][A-Z0-9]{8,}$")
CHAT_PARTICIPATION_VALUES = {"strict", "social", "muted"}

# Default GitHub Projects status names used when no custom lane mapping is set.
DEFAULT_LANE_READY = "Todo"
DEFAULT_LANE_WORKING = "In Progress"
DEFAULT_LANE_DONE = "Done"


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


class GitHubUserReference(BaseModel):
    person_id: str
    github_username: str
    github_user_id: int
    git_email: str


class LaneMapInput(BaseModel):
    """Mapping of workflow lanes to GitHub Project Status option names.

    Blank values fall back to the GitHub Projects defaults (the persistence
    layer strips empty strings, so a lane name cannot be stored empty). If the
    resulting working lane does not exist on the board, the workflow simply
    does not move tickets on start; runtime/diagnostics surface that as a
    warning rather than an error. ``ready`` and ``done`` must differ.
    """

    ready: str = DEFAULT_LANE_READY
    working: str = DEFAULT_LANE_WORKING
    done: str = DEFAULT_LANE_DONE

    @field_validator("ready", "working", "done")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _apply_defaults(self) -> LaneMapInput:
        if not self.ready:
            self.ready = DEFAULT_LANE_READY
        if not self.working:
            self.working = DEFAULT_LANE_WORKING
        if not self.done:
            self.done = DEFAULT_LANE_DONE
        if self.ready == self.done:
            raise ValueError("ready and done lanes must be different")
        return self

    def to_config(self) -> dict[str, str]:
        """Serialize to the ``lane_map`` mapping stored in project.yml."""
        return {"ready": self.ready, "working": self.working, "done": self.done}

    @classmethod
    def from_config(cls, raw: object) -> LaneMapInput:
        """Build from a stored ``lane_map`` mapping, tolerating missing keys."""
        if not isinstance(raw, dict):
            return cls()
        values: dict[str, str] = {}
        for key in ("ready", "working", "done"):
            if key in raw and raw[key] is not None:
                values[key] = str(raw[key])
        return cls(**values)


class GitHubProjectInput(BaseModel):
    """GitHub Project identity fields shared by project setup / update inputs."""

    owner: str = ""
    project_id: str = ""
    github_project_url: str = ""
    lane_map: LaneMapInput = Field(default_factory=LaneMapInput)


class ProjectSetupInput(GitHubProjectInput):
    config_dir: Path
    env_file_path: Path
    env_file_option: str = Field(pattern="^(skip|append|overwrite)$")
    language: str
    description: str = ""
    llm_api_type: str = ""
    cli_agent: str = ""
    provider_api_keys: dict[str, str] = Field(default_factory=dict)

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
            self.owner,
            self.project_id,
            self.github_project_url,
        ]
        if any(github_fields) and not all(github_fields):
            raise ValueError(
                "owner, project_id and github_project_url "
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
    llm_api_type: str = ""
    cli_agent: str = ""
    github_enabled: bool
    github_project_url: str = ""
    lane_map: LaneMapInput = Field(default_factory=LaneMapInput)
    provider_api_keys: dict[str, bool] = Field(default_factory=dict)


class ProjectUpdateInput(GitHubProjectInput):
    config_dir: Path
    env_file_path: Path
    language: str
    description: str = ""
    llm_api_type: str = ""
    cli_agent: str = ""
    github_enabled: bool = False
    provider_api_keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in {"en", "ja"}:
            raise ValueError("language must be one of: en, ja")
        return value

    @model_validator(mode="after")
    def validate_github_fields(self) -> ProjectUpdateInput:
        github_fields = [
            self.owner,
            self.project_id,
            self.github_project_url,
        ]
        if self.github_enabled and not all(github_fields):
            raise ValueError(
                "owner, project_id and github_project_url "
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
    github_account_type: str = ""
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
    slack_user_id: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channels: list[str] = Field(default_factory=list)
    slack_channel_participation: dict[str, str] = Field(default_factory=dict)
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
    github_account_type: str = ""
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
    slack_user_id: str = ""
    has_slack_bot_token: bool = False
    has_slack_app_token: bool = False
    slack_channels: list[str] = Field(default_factory=list)
    slack_channel_participation: dict[str, str] = Field(default_factory=dict)
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)
    avatar_timestamp: int = 0


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
        owner = str(ticket_manager.get("owner", ""))
        project_id = str(ticket_manager.get("project_id", ""))
        github_project_url = str(ticket_manager.get("url", ""))
        github_enabled = bool(owner and project_id and github_project_url)
        lane_map = LaneMapInput.from_config(ticket_manager.get("lane_map"))

        from guildbotics.app_api.llm_providers import provider_env_keys

        env_values = (
            dict(dotenv_values(env_file_path)) if env_file_path.exists() else {}
        )
        provider_api_keys = {
            provider: bool(env_values.get(env_var))
            for provider, env_var in provider_env_keys(config_dir).items()
        }
        return ProjectConfigSnapshot(
            config_dir=config_dir,
            env_file_path=env_file_path,
            language=str(project_data.get("language", "en")),
            description=str(project_data.get("description", "")),
            llm_api_type=self._infer_llm_api_type(model_mapping),
            cli_agent=self._infer_cli_agent(cli_mapping),
            github_enabled=github_enabled,
            github_project_url=github_project_url,
            lane_map=lane_map,
            provider_api_keys=provider_api_keys,
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

    def write_project(self, config: ProjectSetupInput) -> ProjectSetupResult:
        files: list[CreatedFile] = []

        project_config_file = config.config_dir / "team/project.yml"
        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(project_config_file, self.build_project_config(config))
        files.append(CreatedFile(path=project_config_file, action="create"))

        model_mapping_template = get_template_path() / "intelligences/model_mapping.yml"
        model_mapping: dict = cast(dict, load_yaml_file(model_mapping_template))
        model_mapping["default"] = self._resolve_model_default(
            model_mapping, config.llm_api_type
        )
        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="create"))

        cli_mapping_template = (
            get_template_path() / "intelligences/cli_agent_mapping.yml"
        )
        cli_mapping: dict = cast(dict, load_yaml_file(cli_mapping_template))
        cli_mapping["default"] = cli_mapping.get(
            config.cli_agent, f"{config.cli_agent}-cli.yml"
        )
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
                "lane_map": config.lane_map.to_config(),
            }
            services["code_hosting_service"] = {
                "name": "GitHub",
                "owner": config.owner,
            }
        else:
            services.pop("ticket_manager", None)
            services.pop("code_hosting_service", None)

        if services:
            project_data["services"] = services
        else:
            project_data.pop("services", None)

        # ``repositories`` is no longer part of the schema; drop any stale entry
        # left by older configurations.
        project_data.pop("repositories", None)

        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(project_config_file, project_data)
        files.append(CreatedFile(path=project_config_file, action="update"))

        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        model_mapping = self._load_mapping(
            model_mapping_file,
            get_template_path() / "intelligences/model_mapping.yml",
        )
        model_mapping["default"] = self._resolve_model_default(
            model_mapping, config.llm_api_type
        )
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="update"))

        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        cli_mapping = self._load_mapping(
            cli_mapping_file,
            get_template_path() / "intelligences/cli_agent_mapping.yml",
        )
        cli_mapping["default"] = cli_mapping.get(
            config.cli_agent, f"{config.cli_agent}-cli.yml"
        )
        cli_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(cli_mapping_file, cli_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        from guildbotics.app_api.llm_providers import provider_env_keys

        env_keys = provider_env_keys(config.config_dir)
        env_updates = {
            env_keys[provider]: value
            for provider, value in config.provider_api_keys.items()
            if value and provider in env_keys
        }
        if env_updates:
            env_file_existed = config.env_file_path.exists()
            env_values = (
                dict(dotenv_values(config.env_file_path)) if env_file_existed else {}
            )
            env_values.update(env_updates)
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

        if config.github_project_url:
            project["services"] = {
                "ticket_manager": {
                    "name": "GitHub",
                    "owner": config.owner,
                    "project_id": str(config.project_id),
                    "url": config.github_project_url,
                    "lane_map": config.lane_map.to_config(),
                },
                "code_hosting_service": {
                    "name": "GitHub",
                    "owner": config.owner,
                },
            }
        return project

    def render_env_file(self, config: ProjectSetupInput) -> str:
        from guildbotics.app_api.llm_providers import discover_llm_providers

        key_lines = [
            f"{provider.api_key_env}={config.provider_api_keys.get(provider.provider, '')}"
            for provider in discover_llm_providers(config.config_dir)
            if provider.api_key_env
        ]
        tail = (TEMPLATE_PATH / ".env.example").read_text()
        return "\n".join(key_lines) + "\n\n" + tail

    def _load_mapping(self, file_path: Path, template_path: Path) -> dict:
        if file_path.exists():
            return cast(dict, load_yaml_file(file_path))
        return cast(dict, load_yaml_file(template_path))

    def _resolve_model_default(self, mapping: dict, provider: str) -> str:
        # Honor an explicit per-provider slot when one exists, otherwise fall back
        # to the provider's conventional ``models/<provider>/default.yml`` file.
        # The mapping no longer keeps provider-named index entries, so a bare
        # ``mapping[provider]`` lookup would raise ``KeyError``. The default file
        # ships as a template (matching the desktop editor's slot convention), so
        # swapping a provider's default model is just a data change to that file.
        existing = mapping.get(provider)
        if existing:
            return str(existing)
        return f"models/{provider}/default.yml"

    def _infer_llm_api_type(self, mapping: dict) -> str:
        # The default model path is ``models/<provider>/<file>.yml``, so the
        # provider is simply the second path segment (matches how the rest of the
        # stack derives provider from a model path).
        parts = str(mapping.get("default", "")).split("/")
        return parts[1] if len(parts) > 1 else ""

    def _infer_cli_agent(self, mapping: dict) -> str:
        # The default points at ``<agent>-cli.yml``, so the agent name is the
        # file stem without the ``-cli`` suffix.
        name = str(mapping.get("default", "")).removesuffix(".yml")
        return name.removesuffix("-cli")


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
        person_type = str(person_data.get("person_type", ""))
        github_account_type = str(account_info.get("github_account_type", ""))
        if not github_account_type and person_type in {
            "human",
            "machine_user",
            "github_apps",
            "proxy_agent",
        }:
            github_account_type = person_type
        configured_roles = profile.get("roles", {}) if isinstance(profile, dict) else {}
        character = profile.get("character", {}) if isinstance(profile, dict) else {}
        roles = (
            list(configured_roles.keys()) if isinstance(configured_roles, dict) else []
        )
        character_data = character if isinstance(character, dict) else {}

        channels: list[str] = []
        channel_participation: dict[str, str] = {}
        raw_channels = person_data.get("message_channels", [])
        if isinstance(raw_channels, list):
            for channel in raw_channels:
                if (
                    not isinstance(channel, dict)
                    or str(channel.get("service", "")).lower() != "slack"
                ):
                    continue
                channel_name = str(channel.get("name", "")).strip()
                if not channel_name:
                    continue
                channels.append(channel_name)
                chat = channel.get("chat", {})
                participation = (
                    chat.get("participation", "strict")
                    if isinstance(chat, dict)
                    else "strict"
                )
                channel_participation[channel_name] = _chat_participation(participation)

        env = self._read_env_values(env_file_path)
        env_prefix = self._person_env_prefix(person_id)

        avatar_timestamp = 0
        from guildbotics.utils.avatar import find_avatar_file

        avatar_path = find_avatar_file(config_dir, person_id)
        if avatar_path is not None:
            with contextlib.suppress(Exception):
                avatar_timestamp = int(avatar_path.stat().st_mtime)

        return PersonConfigSnapshot(
            person_id=str(person_data.get("person_id", person_id)),
            person_name=str(person_data.get("name", "")),
            person_type=person_type,
            github_account_type=github_account_type,
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
            slack_user_id=str(account_info.get("slack_user_id", "")),
            has_slack_bot_token=bool(env.get(f"{env_prefix}_SLACK_BOT_TOKEN")),
            has_slack_app_token=bool(env.get(f"{env_prefix}_SLACK_APP_TOKEN")),
            slack_channels=channels,
            slack_channel_participation=channel_participation,
            routine_commands=[
                str(command).strip()
                for command in person_data.get("routine_commands", [])
                if str(command).strip()
            ],
            task_schedules=_read_task_schedules(person_data.get("task_schedules", [])),
            avatar_timestamp=avatar_timestamp,
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
        role_overrides: dict[str, dict[str, str]] = {
            role.rstrip(":"): {} for role in config.roles
        }
        profile: dict[str, Any] = {
            "roles": role_overrides,
        }
        if config.character:
            profile["character"] = config.character
        person_config = {
            "person_id": config.person_id,
            "name": config.person_name,
            "is_active": False if config.person_type == "human" else config.is_active,
            "person_type": config.person_type,
            "account_info": {
                "github_account_type": config.github_account_type,
                "github_username": config.github_username,
                "git_user": config.person_name,
                "git_email": config.git_email,
                "slack_user_id": config.slack_user_id,
            },
            "profile": profile,
            "speaking_style": config.speaking_style,
            "relationships": config.relationships,
        }
        if config.slack_channels:
            slack_channel_participation = {
                _normalize_slack_channel_ref(channel): _chat_participation(
                    participation
                )
                for channel, participation in config.slack_channel_participation.items()
            }
            person_config["message_channels"] = [
                self._build_slack_message_channel(
                    channel,
                    person_id=config.person_id,
                    participation=slack_channel_participation.get(
                        _normalize_slack_channel_ref(channel), "strict"
                    ),
                )
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
        self, channel: str, *, person_id: str, participation: str = "strict"
    ) -> dict[str, Any]:
        channel_ref = channel.strip().lstrip("#")
        chat: dict[str, Any] = {
            "enabled": True,
            "participation": _chat_participation(participation),
        }
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


def _normalize_slack_channel_ref(channel: str) -> str:
    return channel.strip().lstrip("#")


def _chat_participation(value: object) -> str:
    participation = str(value or "strict").strip().lower()
    if participation in CHAT_PARTICIPATION_VALUES:
        return participation
    return "strict"
