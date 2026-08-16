from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import requests  # type: ignore
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from guildbotics.editions.simple.simple_edition import DEFAULT_ROUTINE_COMMAND
from guildbotics.entities.team import Person
from guildbotics.intelligences.cli_agents import (
    cli_agent_default_path,
    cli_agent_name_from_path,
)
from guildbotics.utils.fileio import (
    get_template_path,
    load_yaml_file,
    save_yaml_file,
)
from guildbotics.utils.secret_store import (
    SecretStore,
    resolve_secret_store,
)
from guildbotics.utils.shared_write_lock import shared_write_operation

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
# A person ID is also a directory name under ``team/members``, so it must not
# carry separators or traversal segments.
PERSON_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
PERSON_ID_REQUIREMENT = "person_id must contain only lowercase letters, digits, _ or -"

# Default GitHub Projects status names used when no custom lane mapping is set.
DEFAULT_LANE_READY = "Todo"
DEFAULT_LANE_WORKING = "In Progress"
DEFAULT_LANE_DONE = "Done"


def is_valid_person_id(value: str) -> bool:
    """Return whether a person ID is usable as a member directory name."""
    return bool(PERSON_ID_PATTERN.match(value))


def github_app_key_dir() -> Path:
    """OS temporary directory for PEM files from GitHub App registration.

    Generated keys are absorbed into the OS secret store and then deleted.
    User-supplied key files live elsewhere and are never touched.
    """
    return Path(tempfile.gettempdir()) / "guildbotics-github-apps"


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
    def validate_github_fields(self) -> ProjectSetupInput:
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
        if not is_valid_person_id(value):
            raise ValueError(PERSON_ID_REQUIREMENT)
        return value

    @field_validator("person_name")
    @classmethod
    def validate_person_name(cls, value: str) -> str:
        if not value:
            raise ValueError("person_name is required")
        return value


class PersonSetupResult(BaseModel):
    files: list[CreatedFile]


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
    has_github_installation_id: bool = False
    has_github_app_id: bool = False
    has_github_private_key: bool = False
    has_github_access_token: bool = False
    slack_user_id: str = ""
    has_slack_bot_token: bool = False
    has_slack_app_token: bool = False
    slack_channels: list[str] = Field(default_factory=list)
    slack_channel_participation: dict[str, str] = Field(default_factory=dict)
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)
    avatar_timestamp: int = 0
    # Config-relative path -> revision, to be sent back with the save so an
    # edit made elsewhere since this read is refused instead of overwritten.
    revisions: dict[str, str] = Field(default_factory=dict)


class PersonUpdateInput(PersonSetupInput):
    original_person_id: str
    # Revisions this form was composed against; empty for a member being added.
    expected_revisions: dict[str, str] = Field(default_factory=dict)


#: Config files the project screen reads and writes, relative to the config
#: directory. The screen saves all of them together, so a stale-write check has
#: to cover the set rather than the one file the user visibly edited.
PROJECT_CONFIG_PATHS = (
    "team/project.yml",
    "intelligences/model_mapping.yml",
    "intelligences/cli_agent_mapping.yml",
)


def person_config_paths(person_id: str) -> tuple[str, ...]:
    """Return the config files the member screen reads and writes."""
    return (f"team/members/{person_id}/person.yml",)


def _project_config_file(config_dir: Path) -> Path:
    return config_dir / "team/project.yml"


def _person_config_dir(config_dir: Path, person_id: str) -> Path:
    return config_dir / f"team/members/{person_id}"


def _person_config_file(config_dir: Path, person_id: str) -> Path:
    return _person_config_dir(config_dir, person_id) / "person.yml"


def _stored_default_person_id(config_dir: Path) -> str:
    project_file = _project_config_file(config_dir)
    if not project_file.exists():
        return ""
    project_data = cast(dict, load_yaml_file(project_file))
    return str(project_data.get("default_person_id", ""))


def _retarget_default_person_id(
    config_dir: Path, person_id: str, new_person_id: str
) -> list[CreatedFile]:
    """Follow a member rename or deletion in the default executor setting.

    Args:
        config_dir: Workspace config directory holding ``team/project.yml``.
        person_id: Member the setting may currently point at.
        new_person_id: Member to point at instead; empty clears the setting.

    Returns:
        list[CreatedFile]: The updated project file, or an empty list when the
        setting pointed at another member.
    """
    if _stored_default_person_id(config_dir) != person_id:
        return []
    updated = _write_default_person_id(config_dir, new_person_id)
    return [updated] if updated else []


def _write_default_person_id(config_dir: Path, person_id: str) -> CreatedFile | None:
    """Point the team's default command executor at a member.

    Args:
        config_dir: Workspace config directory holding ``team/project.yml``.
        person_id: Member to make the default; an empty value clears the setting.

    Returns:
        CreatedFile | None: The updated project file, or ``None`` when the
        stored value already matched and nothing was written.
    """
    project_file = _project_config_file(config_dir)
    project_data = (
        cast(dict, load_yaml_file(project_file)) if project_file.exists() else {}
    )
    if str(project_data.get("default_person_id", "")) == person_id:
        return None
    if person_id:
        project_data["default_person_id"] = person_id
    else:
        project_data.pop("default_person_id", None)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(project_file, project_data)
    return CreatedFile(path=project_file, action="update")


class SimpleProjectSetupService:
    """Create and change a workspace's project-level config.

    The writing operations are marked
    :func:`~guildbotics.utils.shared_write_lock.shared_write_operation`: each
    lays down several config files that only make sense together, and several
    of them decide what to write by reading the config already there. Either
    on its own makes the span the whole method rather than the individual
    writes, which is all the port can see.

    That decorator locks the selected workspace, while these methods take the
    config directory as an argument. The two are the same workspace for every
    caller today -- the Desktop backend selects it before calling, and the CLI
    resolves it the same way -- and a caller that means a different one has to
    take the lock for that workspace itself.
    """

    def read_project_config(self, *, config_dir: Path) -> ProjectConfigSnapshot:
        project_file = _project_config_file(config_dir)
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

        from guildbotics.intelligences.llm_providers import provider_env_keys

        store = resolve_secret_store(config_dir)
        provider_api_keys = {
            provider: bool(store.get(env_var))
            for provider, env_var in provider_env_keys(config_dir).items()
        }
        return ProjectConfigSnapshot(
            config_dir=config_dir,
            language=str(project_data.get("language", "en")),
            description=str(project_data.get("description", "")),
            llm_api_type=self._infer_llm_api_type(model_mapping),
            cli_agent=self._infer_cli_agent(cli_mapping),
            github_enabled=github_enabled,
            github_project_url=github_project_url,
            lane_map=lane_map,
            provider_api_keys=provider_api_keys,
        )

    @shared_write_operation
    def set_default_person(
        self, *, config_dir: Path, person_id: str
    ) -> ProjectSetupResult:
        """Store the member used when a command execution names no person.

        Args:
            config_dir: Workspace config directory holding ``team/project.yml``.
            person_id: Member to make the default; an empty value clears it.

        Returns:
            ProjectSetupResult: The files touched by the update.

        Raises:
            SetupServiceError: If the ID is malformed, has no configuration, or
                names a member that cannot execute commands.
        """
        if person_id:
            if not is_valid_person_id(person_id):
                raise SetupServiceError("invalid_person_id", PERSON_ID_REQUIREMENT)
            person_file = _person_config_file(config_dir, person_id)
            if not person_file.exists():
                raise SetupServiceError(
                    "person_not_found", "Member config was not found."
                )
            person_data = cast(dict, load_yaml_file(person_file))
            if str(person_data.get("person_type", "")) == "human":
                raise SetupServiceError(
                    "person_not_executable",
                    "A human member cannot be the default command executor.",
                )
        updated = _write_default_person_id(config_dir, person_id)
        return ProjectSetupResult(files=[updated] if updated else [])

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

    @shared_write_operation
    def write_project(self, config: ProjectSetupInput) -> ProjectSetupResult:
        files: list[CreatedFile] = []

        project_config_file = _project_config_file(config.config_dir)
        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(project_config_file, self.build_project_config(config))
        files.append(CreatedFile(path=project_config_file, action="create"))

        model_mapping_template = get_template_path() / "intelligences/model_mapping.yml"
        model_mapping: dict = cast(dict, load_yaml_file(model_mapping_template))
        self._set_default_model_mapping(model_mapping, config.llm_api_type)
        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="create"))

        cli_mapping_template = (
            get_template_path() / "intelligences/cli_agent_mapping.yml"
        )
        cli_mapping: dict = cast(dict, load_yaml_file(cli_mapping_template))
        self._set_default_cli_mapping(cli_mapping, config.cli_agent)
        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        save_yaml_file(cli_mapping_file, cli_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="create"))

        native_policy_file = config.config_dir / "intelligences/native_agent_policy.yml"
        if not native_policy_file.exists():
            native_policy_template = (
                get_template_path() / "intelligences/native_agent_policy.yml"
            )
            native_policy_file.write_text(
                native_policy_template.read_text(encoding="utf-8"), encoding="utf-8"
            )
            files.append(CreatedFile(path=native_policy_file, action="create"))

        # Tool definitions live at `cli_agents/<tool>/default.yml`, so the copy
        # walks one level down rather than the directory root.
        cli_agent_config_src_dir = get_template_path() / "intelligences/cli_agents"
        cli_agent_config_dst_dir = config.config_dir / "intelligences/cli_agents"
        for src_file in sorted(cli_agent_config_src_dir.glob("*/*.yml")):
            dst_file = cli_agent_config_dst_dir / src_file.parent.name / src_file.name
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.write_text(src_file.read_text())
            files.append(CreatedFile(path=dst_file, action="create"))

        files.extend(self.ensure_sample_commands(config.config_dir, config.language))

        store = resolve_secret_store(config.config_dir, create_default=True)
        store.ensure_initialized()
        files.append(CreatedFile(path=store.location, action="create"))
        self._store_provider_api_keys(config, store)

        return ProjectSetupResult(files=files)

    def _store_provider_api_keys(
        self, config: ProjectSetupInput | ProjectUpdateInput, store: SecretStore
    ) -> None:
        """Store non-empty provider API keys in the OS secret store."""
        from guildbotics.intelligences.llm_providers import provider_env_keys

        env_keys = provider_env_keys(config.config_dir)
        for provider, value in config.provider_api_keys.items():
            if value and provider in env_keys:
                store.set(env_keys[provider], value)

    @shared_write_operation
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

    @shared_write_operation
    def update_project(self, config: ProjectUpdateInput) -> ProjectSetupResult:
        files: list[CreatedFile] = []
        project_config_file = _project_config_file(config.config_dir)
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
        self._set_default_model_mapping(model_mapping, config.llm_api_type)
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="update"))

        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        cli_mapping = self._load_mapping(
            cli_mapping_file,
            get_template_path() / "intelligences/cli_agent_mapping.yml",
        )
        self._set_default_cli_mapping(cli_mapping, config.cli_agent)
        cli_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(cli_mapping_file, cli_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        from guildbotics.intelligences.llm_providers import provider_env_keys

        env_keys = provider_env_keys(config.config_dir)
        env_updates = {
            env_keys[provider]: value
            for provider, value in config.provider_api_keys.items()
            if value and provider in env_keys
        }
        if env_updates:
            store = resolve_secret_store(config.config_dir)
            location_existed = store.location.exists()
            for key, value in env_updates.items():
                store.set(key, value)
            files.append(
                CreatedFile(
                    path=store.location,
                    action="update" if location_existed else "create",
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

    def _load_mapping(self, file_path: Path, template_path: Path) -> dict:
        if file_path.exists():
            return cast(dict, load_yaml_file(file_path))
        return cast(dict, load_yaml_file(template_path))

    def _set_default_model_mapping(self, mapping: dict, provider: str) -> None:
        if provider:
            mapping["default"] = self._resolve_model_default(mapping, provider)

    def _set_default_cli_mapping(self, mapping: dict, agent: str) -> None:
        # A tool is named by its definition path, the same way a model is, so
        # the catalog name selected during setup becomes that tool's default.
        if agent:
            mapping["default"] = cli_agent_default_path(agent)

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
        # The default path is ``cli_agents/<tool>/<slot>.yml``, so the tool is
        # the directory (matching how the provider is read off a model path).
        return cli_agent_name_from_path(str(mapping.get("default", "")))


class SimplePersonSetupService:
    """Create, change, and remove a workspace's members.

    The writing operations are marked
    :func:`~guildbotics.utils.shared_write_lock.shared_write_operation`: each
    lays down several config files that only make sense together, and several
    of them decide what to write by reading the config already there. Either
    on its own makes the span the whole method rather than the individual
    writes, which is all the port can see.

    That decorator locks the selected workspace, while these methods take the
    config directory as an argument. The two are the same workspace for every
    caller today -- the Desktop backend selects it before calling, and the CLI
    resolves it the same way -- and a caller that means a different one has to
    take the lock for that workspace itself.
    """

    def read_slack_tokens(self, *, config_dir: Path, person_id: str) -> tuple[str, str]:
        """Return the member's stored Slack bot and app tokens.

        Resolved the same way as ``has_slack_bot_token`` in the member
        snapshot, so a token the GUI reports as saved is the token returned
        here. Missing members simply have no secrets and yield empty strings.
        """
        store = resolve_secret_store(config_dir)
        prefix = self._person_env_prefix(person_id)
        return (
            store.get(f"{prefix}_SLACK_BOT_TOKEN") or "",
            store.get(f"{prefix}_SLACK_APP_TOKEN") or "",
        )

    def read_person_config(
        self, *, config_dir: Path, person_id: str
    ) -> PersonConfigSnapshot:
        person_file = _person_config_file(config_dir, person_id)
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

        store = resolve_secret_store(config_dir)
        env_prefix = self._person_env_prefix(person_id)

        def secret_value(key: str) -> str:
            return store.get(key) or ""

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
                account_info.get("github_installation_id")
            ),
            github_app_id=_to_int_or_none(account_info.get("github_app_id")),
            has_github_installation_id=bool(account_info.get("github_installation_id")),
            has_github_app_id=bool(account_info.get("github_app_id")),
            has_github_private_key=bool(
                secret_value(f"{env_prefix}_GITHUB_PRIVATE_KEY")
            ),
            has_github_access_token=bool(
                secret_value(f"{env_prefix}_GITHUB_ACCESS_TOKEN")
            ),
            slack_user_id=str(account_info.get("slack_user_id", "")),
            has_slack_bot_token=bool(secret_value(f"{env_prefix}_SLACK_BOT_TOKEN")),
            has_slack_app_token=bool(secret_value(f"{env_prefix}_SLACK_APP_TOKEN")),
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

    @shared_write_operation
    def write_person(self, config: PersonSetupInput) -> PersonSetupResult:
        files: list[CreatedFile] = []
        person_config_file = _person_config_file(config.config_dir, config.person_id)
        person_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(
            person_config_file,
            self.build_person_config(config, include_initial_defaults=True),
        )
        files.append(CreatedFile(path=person_config_file, action="create"))

        store = resolve_secret_store(config.config_dir)
        secrets = self.build_person_secrets(config)
        for key, value in secrets.items():
            store.set(key, value)
        stored_pem = self._store_private_key_content(store, config)
        if secrets or stored_pem:
            files.append(CreatedFile(path=store.location, action="update"))

        return PersonSetupResult(files=files)

    @shared_write_operation
    def update_person(self, config: PersonUpdateInput) -> PersonSetupResult:
        files: list[CreatedFile] = []
        original_person_file = _person_config_file(
            config.config_dir, config.original_person_id
        )
        if not original_person_file.exists():
            raise SetupServiceError("person_not_found", "Member config was not found.")

        old_person_dir = original_person_file.parent
        new_person_dir = _person_config_dir(config.config_dir, config.person_id)
        if old_person_dir != new_person_dir and new_person_dir.exists():
            raise SetupServiceError("person_id_conflict", "Member ID already exists.")

        # SecretStore first: shared key metadata moves independently of local
        # value freshness, and a store failure leaves the member config
        # untouched so the same update can be retried.
        store = resolve_secret_store(config.config_dir)
        old_prefix = self._person_env_prefix(config.original_person_id)
        new_prefix = self._person_env_prefix(config.person_id)
        renamed = False
        if old_prefix != new_prefix:
            indexed = set(store.keys())
            for suffix in Person.SECRET_ENV_SUFFIXES:
                old_key = f"{old_prefix}_{suffix}"
                if old_key in indexed:
                    store.rename(old_key, f"{new_prefix}_{suffix}")
                    renamed = True
        pending_secrets = self.build_person_secrets(config)
        stored_pem = self._store_private_key_content(store, config)
        for key, value in pending_secrets.items():
            store.set(key, value)
        if renamed or pending_secrets or stored_pem:
            files.append(CreatedFile(path=store.location, action="update"))

        if old_person_dir != new_person_dir:
            new_person_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_person_dir), str(new_person_dir))
            files.extend(
                _retarget_default_person_id(
                    config.config_dir, config.original_person_id, config.person_id
                )
            )

        person_file = new_person_dir / "person.yml"
        person_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(person_file, self.build_person_config(config))
        files.append(CreatedFile(path=person_file, action="update"))

        return PersonSetupResult(files=files)

    @shared_write_operation
    def delete_person(self, *, config_dir: Path, person_id: str) -> PersonSetupResult:
        files: list[CreatedFile] = []
        person_dir = _person_config_dir(config_dir, person_id)
        person_file = person_dir / "person.yml"
        if not person_file.exists():
            raise SetupServiceError("person_not_found", "Member config was not found.")

        # SecretStore first: indexed keys are removed whether or not this
        # device holds their current generation, and a store failure leaves
        # the member config intact so the deletion can be retried.
        store = resolve_secret_store(config_dir)
        person_keys = set(self._person_secret_env_keys(person_id))
        stored_keys = store.keys()
        indexed = [key for key in stored_keys if key in person_keys]
        for key in indexed:
            store.delete(key)
        if indexed:
            files.append(CreatedFile(path=store.location, action="update"))

        shutil.rmtree(person_dir)
        files.append(CreatedFile(path=person_file, action="delete"))
        files.extend(_retarget_default_person_id(config_dir, person_id, ""))
        return PersonSetupResult(files=files)

    def build_person_config(
        self, config: PersonSetupInput, *, include_initial_defaults: bool = False
    ) -> dict:
        is_human = config.person_type == "human"
        role_overrides: dict[str, dict[str, str]] = {
            role.rstrip(":"): {} for role in config.roles
        }
        profile: dict[str, Any] = {
            "roles": role_overrides,
        }
        if config.character and not is_human:
            profile["character"] = config.character
        person_config = {
            "person_id": config.person_id,
            "name": config.person_name,
            "is_active": False if is_human else config.is_active,
            "person_type": config.person_type,
            "account_info": {
                "github_account_type": "human"
                if is_human
                else config.github_account_type,
                "github_username": config.github_username,
                "git_user": config.person_name,
                "git_email": config.git_email,
                "slack_user_id": config.slack_user_id,
                **(
                    {"github_app_id": str(config.github_app_id)}
                    if config.github_app_id is not None
                    else {}
                ),
                **(
                    {"github_installation_id": str(config.github_installation_id)}
                    if config.github_installation_id is not None
                    else {}
                ),
            },
            "profile": profile,
            "speaking_style": "" if is_human else config.speaking_style,
            "relationships": "" if is_human else config.relationships,
        }
        if config.slack_channels and not is_human:
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
        routine_commands = (
            []
            if is_human
            else [
                command.strip()
                for command in config.routine_commands
                if command.strip()
            ]
        )
        if (
            include_initial_defaults
            and not routine_commands
            and self._should_seed_default_routine(config)
        ):
            routine_commands = [DEFAULT_ROUTINE_COMMAND]
        if routine_commands:
            person_config["routine_commands"] = routine_commands
        task_schedules = (
            []
            if is_human
            else [
                schedule.model_dump()
                for schedule in config.task_schedules
                if schedule.command.strip() and schedule.schedules
            ]
        )
        if task_schedules:
            person_config["task_schedules"] = task_schedules
        return person_config

    @staticmethod
    def _should_seed_default_routine(config: PersonSetupInput) -> bool:
        if config.person_type == "human":
            return False
        github_account_type = (config.github_account_type or config.person_type).strip()
        if github_account_type in {"", "none", "human"}:
            return bool(config.github_username.strip())
        return github_account_type in {"machine_user", "github_apps", "proxy_agent"}

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

    def build_person_secrets(self, config: PersonSetupInput) -> dict[str, str]:
        """Return the secret values a member save stores in the secret store."""
        if config.person_type == "human":
            return {}
        prefix = self._person_env_prefix(config.person_id)
        values = {
            f"{prefix}_GITHUB_ACCESS_TOKEN": config.github_access_token,
            f"{prefix}_SLACK_BOT_TOKEN": config.slack_bot_token,
            f"{prefix}_SLACK_APP_TOKEN": config.slack_app_token,
        }
        return {key: value for key, value in values.items() if value}

    def _person_env_prefix(self, person_id: str) -> str:
        return person_id.replace("-", "_").upper()

    def _person_secret_env_keys(self, person_id: str) -> list[str]:
        prefix = self._person_env_prefix(person_id)
        return [f"{prefix}_{suffix}" for suffix in Person.SECRET_ENV_SUFFIXES]

    def _store_private_key_content(
        self, store: SecretStore, config: PersonSetupInput
    ) -> bool:
        """Copy the GitHub App PEM into the keychain.

        Runtime prefers this content over the ``*_GITHUB_PRIVATE_KEY_PATH``
        file, so the plaintext PEM can be deleted afterwards: a flow-generated
        file is removed here, a user-supplied file is left to the user. An
        unreadable path is ignored.
        """
        if not config.github_private_key_path:
            return False
        key_file = Path(config.github_private_key_path).expanduser()
        try:
            pem = key_file.read_text()
        except OSError:
            return False
        prefix = self._person_env_prefix(config.person_id)
        store.set(f"{prefix}_GITHUB_PRIVATE_KEY", pem)
        self._discard_generated_key_file(key_file)
        return True

    def _discard_generated_key_file(self, key_file: Path) -> None:
        """Delete a registration-flow PEM once the keychain holds its content."""
        generated_dir = github_app_key_dir().resolve(strict=False)
        if key_file.resolve(strict=False).is_relative_to(generated_dir):
            key_file.unlink(missing_ok=True)


def _normalize_slack_channel_ref(channel: str) -> str:
    return channel.strip().lstrip("#")


def _chat_participation(value: object) -> str:
    participation = str(value or "strict").strip().lower()
    if participation in CHAT_PARTICIPATION_VALUES:
        return participation
    return "strict"
