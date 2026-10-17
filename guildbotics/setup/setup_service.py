from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml import YAMLError

from guildbotics.entities.team import Service
from guildbotics.environment.contract import (
    AccessContractError,
    validate_workspace_location,
)
from guildbotics.integrations.chat_profile import chat_participation
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.integrations.provider import MemberAccount, SavedMember, SetupError
from guildbotics.intelligences.cli_agents import (
    cli_agent_default_path,
    cli_agent_name_from_path,
)
from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND
from guildbotics.utils.avatar import avatar_in, find_avatar_file
from guildbotics.utils.fileio import (
    get_template_path,
    load_yaml_file,
    save_yaml_file,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.person_id import (
    PersonEnvPrefixConflictError,
    is_valid_person_id,
    iter_member_config_directories,
    person_config_directory,
    person_secret_prefix,
    stored_person_config_directory,
    validate_member_directory_name,
    validate_person_id,
)
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    resolve_secret_store,
)
from guildbotics.utils.shared_write_lock import shared_write_operation
from guildbotics.utils.workspace_state import register_workspace

BASE_DIR = Path(__file__).parent
TEMPLATE_PATH = BASE_DIR / "templates"
SAMPLE_COMMANDS_PATH = TEMPLATE_PATH / "sample_commands"
CRON_FIELD_COUNT = 5
#: The secrets a member may hold with any provider, by member-relative name.
MEMBER_SECRET_NAMES = frozenset().union(
    *(provider.secret_keys for provider in PROVIDERS.values())
)


def _target_prefix(members: Path, person_id: str, *, exclude: str | None = None) -> str:
    """Reject a proposed ID before touching credentials or member files."""
    try:
        return person_secret_prefix(members, person_id, exclude=exclude)
    except PersonEnvPrefixConflictError as conflict:
        raise SetupError(
            "person_env_prefix_conflict",
            t(
                "member_config.target_prefix_conflict",
                person_id=person_id,
                members=", ".join(path.name for path in conflict.conflicts),
            ),
        ) from conflict


@dataclass(frozen=True)
class _OwnedSecrets:
    """A stored member's own secret keys, and the store to touch them in."""

    store: KeyringSecretStore | None
    prefix: str

    def has(self, name: str) -> bool:
        return bool(self.value(name))

    def value(self, name: str) -> str:
        return (self.store.get(self.prefix + name) if self.store else None) or ""

    def keys(self) -> dict[str, str]:
        """The member's keys, by member-relative name."""
        if self.store is None:
            return {}
        return {name: self.prefix + name for name in sorted(MEMBER_SECRET_NAMES)}


def _owned_secrets(config_dir: Path, person_id: str) -> _OwnedSecrets:
    """No store and no keys while the keys are shared: they are nobody's own."""
    try:
        prefix = person_secret_prefix(config_dir / "team/members", person_id)
    except PersonEnvPrefixConflictError:
        return _OwnedSecrets(None, "")
    return _OwnedSecrets(resolve_secret_store(config_dir), prefix)


def _service_config(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """``project.services`` holds a kind as the name of the provider serving
    it, and the provider's own items, as its setup reads them."""
    service = Service(kind)
    provider = PROVIDERS.get(str(item.get("name", "")).lower())
    if provider is None or getattr(provider, service.value) is None:
        raise ValueError(f"'{item.get('name', '')}' provides no {kind}")
    items = {key: value for key, value in item.items() if key != "name"}
    model = provider.setup.services.get(service) if provider.setup else None
    if model is not None:
        try:
            items = model.model_validate(items).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise ValueError(f"{kind}: {exc}") from exc
    return {"name": provider.name, **items}


def _member_account(name: str, form: object) -> MemberAccount:
    provider = PROVIDERS.get(name)
    if provider is None or provider.setup is None or provider.setup.member is None:
        raise ValueError(f"'{name}' has no member account")
    try:
        return provider.setup.member.model_validate(form)
    except ValidationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _channel(reference: str) -> str:
    """A chat channel as the member screen names it, without the ``#``."""
    return reference.strip().lstrip("#")


def _project_services(config_dir: Path) -> dict[str, Any]:
    project_file = _project_config_file(config_dir)
    data = load_yaml_file(project_file) if project_file.exists() else {}
    return _config_mapping(_config_mapping(data).get("services"))


def _chat_channel(config_dir: Path) -> Callable[[str], dict[str, str]]:
    """How the project's chat names a channel the member screen names."""
    chat = _config_mapping(_project_services(config_dir).get("chat_service"))
    provider = PROVIDERS.get(str(chat.get("name", "")).lower())
    setup = provider.setup if provider else None
    if setup and setup.chat_channel:
        return setup.chat_channel
    return lambda reference: {"channel_name": reference}


class ProjectSetupInput(BaseModel):
    # A pasted API key often carries the newline it was copied with.
    model_config = ConfigDict(str_strip_whitespace=True)

    config_dir: Path
    language: str
    description: str = ""
    llm_api_type: str = ""
    cli_agent: str = ""
    provider_api_keys: dict[str, str] = Field(default_factory=dict)
    #: Each kind of service the project uses: the provider's name and items.
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in {"en", "ja"}:
            raise ValueError("language must be one of: en, ja")
        return value

    @field_validator("services")
    @classmethod
    def validate_services(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        services = {kind: _service_config(kind, item) for kind, item in value.items()}
        for provider in PROVIDERS.values():
            for key in provider.setup.shared if provider.setup else ():
                given = {
                    str(item.get(key))
                    for item in services.values()
                    if item["name"] == provider.name
                }
                if len(given) > 1:
                    raise ValueError(
                        f"{provider.name}: every kind it serves needs the same {key}"
                    )
        return services


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
    services: dict[str, Any] = Field(default_factory=dict)
    provider_api_keys: dict[str, bool] = Field(default_factory=dict)


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
    person_id: str
    person_name: str
    is_active: bool
    git_email: str = ""
    roles: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    relationships: str = ""
    character: dict[str, Any] = Field(default_factory=dict)
    #: The member's account with each provider, by the provider's name.
    accounts: dict[str, Any] = Field(default_factory=dict)
    chat_channels: list[str] = Field(default_factory=list)
    chat_channel_participation: dict[str, str] = Field(default_factory=dict)
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)

    _person_id = field_validator("person_id")(validate_person_id)

    @field_validator("person_name")
    @classmethod
    def validate_person_name(cls, value: str) -> str:
        if not value:
            raise ValueError("person_name is required")
        return value

    @field_validator("chat_channels")
    @classmethod
    def validate_chat_channels(cls, value: list[str]) -> list[str]:
        return [_channel(channel) for channel in value if _channel(channel)]

    @field_validator("chat_channel_participation")
    @classmethod
    def validate_participation(cls, value: dict[str, str]) -> dict[str, str]:
        return {_channel(channel): policy for channel, policy in value.items()}

    @field_validator("accounts")
    @classmethod
    def validate_accounts(cls, value: dict[str, Any]) -> dict[str, MemberAccount]:
        return {name: _member_account(name, form) for name, form in value.items()}


class PersonSetupResult(BaseModel):
    files: list[CreatedFile]


class PersonConfigSummary(BaseModel):
    person_id: str
    name: str
    person_type: str = ""
    is_active: bool
    roles: list[str] = Field(default_factory=list)


class PersonConfigSnapshot(BaseModel):
    person_id: str
    person_name: str
    person_type: str
    is_active: bool
    git_email: str
    roles: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    relationships: str = ""
    character: dict[str, Any] = Field(default_factory=dict)
    #: The member's account with each provider, as its setup shows it.
    accounts: dict[str, Any] = Field(default_factory=dict)
    # Member-relative secret name -> the key that member's value is stored
    # under. The naming belongs to the backend, so the screen that shows a
    # value's synchronization state asks for the name rather than rebuilding it.
    secret_env_keys: dict[str, str] = Field(default_factory=dict)
    chat_channels: list[str] = Field(default_factory=list)
    chat_channel_participation: dict[str, str] = Field(default_factory=dict)
    routine_commands: list[str] = Field(default_factory=list)
    task_schedules: list[PersonTaskScheduleInput] = Field(default_factory=list)
    avatar_timestamp: int = 0
    # Config-relative path -> revision, to be sent back with the save so an
    # edit made elsewhere since this read is refused instead of overwritten.
    revisions: dict[str, str] = Field(default_factory=dict)


class PersonUpdateInput(PersonSetupInput):
    original_person_id: str
    _original_person_id = field_validator("original_person_id")(
        validate_member_directory_name
    )
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
    return ((person_config_directory(person_id) / "person.yml").as_posix(),)


def stored_person_config_paths(name: str) -> tuple[str, ...]:
    """Revisions of existing configuration addressed for repair."""
    return ((stored_person_config_directory(name) / "person.yml").as_posix(),)


def _project_config_file(config_dir: Path) -> Path:
    return config_dir / "team/project.yml"


def _person_config_dir(config_dir: Path, person_id: str) -> Path:
    return config_dir / person_config_directory(person_id)


def stored_person_config_dir(config_dir: Path, name: str) -> Path:
    path = config_dir / stored_person_config_directory(name)
    members = config_dir / "team/members"
    if not any(child.name == name for child in iter_member_config_directories(members)):
        raise SetupError("person_not_found", "Member config was not found.")
    return path


def _config_mapping(value: object) -> dict[str, Any]:
    """Project invalid stored data into an editable form, never into a member."""
    return (
        {key: item for key, item in value.items() if isinstance(key, str)}
        if isinstance(value, dict)
        else {}
    )


def _read_member_form(path: Path) -> dict[str, Any]:
    try:
        return _config_mapping(load_yaml_file(path))
    except YAMLError:
        return {}


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
            raise SetupError("project_not_found", "Project config was not found.")
        project_data = cast(dict, load_yaml_file(project_file))

        model_mapping = self._load_mapping(
            config_dir / "intelligences/model_mapping.yml",
            get_template_path() / "intelligences/model_mapping.yml",
        )
        cli_mapping = self._load_mapping(
            config_dir / "intelligences/cli_agent_mapping.yml",
            get_template_path() / "intelligences/cli_agent_mapping.yml",
        )

        from guildbotics.intelligences.llm_providers import (
            provider_env_keys,
            provider_of,
        )

        store = resolve_secret_store(config_dir)
        provider_api_keys = {
            provider: bool(store.get(env_var))
            for provider, env_var in provider_env_keys(config_dir).items()
        }
        return ProjectConfigSnapshot(
            config_dir=config_dir,
            language=str(project_data.get("language", "en")),
            description=str(project_data.get("description", "")),
            llm_api_type=provider_of(str(model_mapping.get("default", ""))),
            # The default path is ``cli_agents/<tool>/<slot>.yml``, so the tool
            # is the directory (as the provider is read off a model path).
            cli_agent=cli_agent_name_from_path(str(cli_mapping.get("default", ""))),
            # A provider's name, as the runtime reads it, is case-insensitive.
            services={
                kind: {**item, "name": str(item.get("name", "")).lower()}
                for kind, item in _config_mapping(project_data.get("services")).items()
                if isinstance(item, dict)
            },
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
            SetupError: If the ID is malformed, has no configuration, or
                names a member that cannot execute commands.
        """
        if person_id:
            try:
                validate_person_id(person_id)
            except ValueError as exc:
                raise SetupError("invalid_person_id", str(exc)) from exc
            person_file = _person_config_file(config_dir, person_id)
            if not person_file.exists():
                raise SetupError("person_not_found", "Member config was not found.")
            person_data = cast(dict, load_yaml_file(person_file))
            if str(person_data.get("person_type", "")) == "human":
                raise SetupError(
                    "person_not_executable",
                    "A human member cannot be the default command executor.",
                )
        updated = _write_default_person_id(config_dir, person_id)
        return ProjectSetupResult(files=[updated] if updated else [])

    @shared_write_operation
    def write_project(self, config: ProjectSetupInput) -> ProjectSetupResult:
        if (
            config.config_dir.name == "config"
            and config.config_dir.parent.name == ".guildbotics"
        ):
            try:
                register_workspace(
                    validate_workspace_location(config.config_dir.parent.parent)
                )
            except AccessContractError as exc:
                raise SetupError("invalid_workspace_location", str(exc)) from exc
        files: list[CreatedFile] = []

        from guildbotics.utils.safe_paths import inspect_host_path

        inspect_host_path(config.config_dir, create=True)

        # Tool definitions live at `cli_agents/<tool>/default.yml`, so the copy
        # walks one level down rather than the directory root. These templates
        # are copied as bytes rather than read and written as text: a text
        # round trip rewrites the newlines to the ones this OS uses, so the
        # same template would land as CRLF on Windows and LF elsewhere -- and
        # every device that joined a hub would find these files differing for
        # no reason anyone chose.
        cli_agent_config_src_dir = get_template_path() / "intelligences/cli_agents"
        cli_agent_config_dst_dir = config.config_dir / "intelligences/cli_agents"
        for src_file in sorted(cli_agent_config_src_dir.glob("*/*.yml")):
            dst_file = cli_agent_config_dst_dir / src_file.parent.name / src_file.name
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            files.append(CreatedFile(path=dst_file, action="create"))

        files.extend(self.ensure_sample_commands(config.config_dir, config.language))
        files.extend(self._write_project_config(config, update=False))
        return ProjectSetupResult(files=files)

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
    def update_project(self, config: ProjectSetupInput) -> ProjectSetupResult:
        files = self._write_project_config(config, update=True)
        files.extend(self.ensure_sample_commands(config.config_dir, config.language))
        return ProjectSetupResult(files=files)

    def _write_project_config(
        self, config: ProjectSetupInput, *, update: bool
    ) -> list[CreatedFile]:
        """Write ``project.yml``, the default mappings and the API keys.

        A new project starts each file from nothing or its template and pins the
        secret store; an update starts from the files already there and touches
        the store only to store a key.
        """
        from guildbotics.intelligences.llm_providers import provider_env_keys

        action = "update" if update else "create"
        project_file = _project_config_file(config.config_dir)
        project = (
            cast(dict, load_yaml_file(project_file))
            if update and project_file.exists()
            else {}
        )
        project["language"] = config.language
        for key, value in (
            ("description", config.description),
            ("services", config.services),
        ):
            if value:
                project[key] = value
            else:
                project.pop(key, None)
        written = {project_file: project}
        for name, choice, default in (
            ("model_mapping.yml", config.llm_api_type, _model_default),
            # A tool is named by its definition path, the same way a model is,
            # so the catalog name selected during setup becomes its default.
            (
                "cli_agent_mapping.yml",
                config.cli_agent,
                lambda _mapping, agent: cli_agent_default_path(agent),
            ),
        ):
            path = config.config_dir / "intelligences" / name
            template = get_template_path() / "intelligences" / name
            mapping = self._load_mapping(path if update else template, template)
            if choice:
                mapping["default"] = default(mapping, choice)
            written[path] = mapping
        files = []
        for path, data in written.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            save_yaml_file(path, data)
            files.append(CreatedFile(path=path, action=action))

        env_keys = provider_env_keys(config.config_dir)
        api_keys = {
            env_keys[provider]: value
            for provider, value in config.provider_api_keys.items()
            if value and provider in env_keys
        }
        if api_keys or not update:
            store = resolve_secret_store(config.config_dir, create_default=not update)
            store_action = "update" if update and store.location.exists() else "create"
            for key, value in api_keys.items():
                store.set(key, value)
            files.append(CreatedFile(path=store.location, action=store_action))
        return files

    def _load_mapping(self, file_path: Path, template_path: Path) -> dict:
        if file_path.exists():
            return cast(dict, load_yaml_file(file_path))
        return cast(dict, load_yaml_file(template_path))


def _model_default(mapping: dict, provider: str) -> str:
    """The provider's own slot when the mapping has one, else its shipped
    ``models/<provider>/default.yml``."""
    return str(mapping.get(provider) or f"models/{provider}/default.yml")


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

    def list_person_configs(self, *, config_dir: Path) -> list[PersonConfigSummary]:
        """Read the member list without requiring a project config or secrets."""
        members_dir = config_dir / "team/members"
        members = []
        for person_dir in iter_member_config_directories(members_dir):
            person_file = person_dir / "person.yml"
            try:
                validate_member_directory_name(person_dir.name)
            except ValueError:
                continue
            person_data = _read_member_form(person_file)
            profile = _config_mapping(person_data.get("profile"))
            configured_roles = _config_mapping(profile.get("roles"))
            members.append(
                PersonConfigSummary(
                    person_id=person_dir.name,
                    name=str(person_data.get("name", "")),
                    person_type=str(person_data.get("person_type", "")),
                    is_active=bool(person_data.get("is_active", False)),
                    roles=list(configured_roles),
                )
            )
        return members

    def read_person_config(
        self, *, config_dir: Path, person_id: str
    ) -> PersonConfigSnapshot:
        person_file = stored_person_config_dir(config_dir, person_id) / "person.yml"
        person_data = _read_member_form(person_file)
        account_info = _config_mapping(person_data.get("account_info"))
        profile = _config_mapping(person_data.get("profile"))
        roles = list(_config_mapping(profile.get("roles")))
        character_data = _config_mapping(profile.get("character"))
        routine_commands = person_data.get("routine_commands")

        channels: list[str] = []
        channel_participation: dict[str, str] = {}
        raw_channels = person_data.get("message_channels", [])
        if isinstance(raw_channels, list):
            for channel in raw_channels:
                if not isinstance(channel, dict):
                    continue
                channel_name = str(channel.get("name", "")).strip()
                if not channel_name:
                    continue
                channels.append(channel_name)
                channel_participation[channel_name] = chat_participation(
                    _config_mapping(channel.get("chat")).get("participation")
                )

        owned = _owned_secrets(config_dir, person_id)
        accounts = {
            provider.name: provider.setup.member.view(account_info, owned.has)
            for provider in PROVIDERS.values()
            if provider.setup and provider.setup.member
        }
        avatar_timestamp = 0
        avatar_path = (
            find_avatar_file(config_dir, person_id)
            if is_valid_person_id(person_id)
            else None
        )
        if avatar_path is not None:
            with contextlib.suppress(Exception):
                avatar_timestamp = int(avatar_path.stat().st_mtime)

        return PersonConfigSnapshot(
            person_id=person_id,
            person_name=str(person_data.get("name", "")),
            person_type=str(person_data.get("person_type", "")),
            is_active=bool(person_data.get("is_active", False)),
            git_email=str(account_info.get("git_email", "")),
            roles=roles,
            speaking_style=str(person_data.get("speaking_style", "")),
            relationships=str(person_data.get("relationships", "")),
            character=character_data,
            accounts={name: view.model_dump() for name, view in accounts.items()},
            secret_env_keys=owned.keys(),
            chat_channels=channels,
            chat_channel_participation=channel_participation,
            routine_commands=[
                str(command).strip()
                for command in (
                    routine_commands if isinstance(routine_commands, list) else []
                )
                if str(command).strip()
            ],
            task_schedules=_read_task_schedules(person_data.get("task_schedules", [])),
            avatar_timestamp=avatar_timestamp,
        )

    @shared_write_operation
    def write_person(self, config: PersonSetupInput) -> PersonSetupResult:
        """Store the member's secrets, then write its config."""
        # SecretStore first, as in update_person: a store failure leaves no
        # member config claiming credentials it does not have.
        prefix = _target_prefix(config.config_dir / "team/members", config.person_id)
        account_info, secrets = _stored_accounts(config, config.person_id)
        store = resolve_secret_store(config.config_dir)
        for name, value in secrets.items():
            store.set(prefix + name, value)

        person_config_file = _person_config_file(config.config_dir, config.person_id)
        person_config_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(
            person_config_file,
            self.build_person_config(
                config, account_info, include_initial_defaults=True
            ),
        )
        _saved(config)
        files = [CreatedFile(path=person_config_file, action="create")]
        if secrets:
            files.append(CreatedFile(path=store.location, action="update"))
        return PersonSetupResult(files=files)

    @shared_write_operation
    def update_person(self, config: PersonUpdateInput) -> PersonSetupResult:
        """Move and update the member's secrets, then its config."""
        files: list[CreatedFile] = []
        original_person_file = (
            stored_person_config_dir(config.config_dir, config.original_person_id)
            / "person.yml"
        )
        if not original_person_file.exists():
            raise SetupError("person_not_found", "Member config was not found.")

        old_person_dir = original_person_file.parent
        new_person_dir = _person_config_dir(config.config_dir, config.person_id)
        new_prefix = _target_prefix(
            config.config_dir / "team/members",
            config.person_id,
            exclude=config.original_person_id,
        )
        if new_person_dir.exists() and not old_person_dir.samefile(new_person_dir):
            raise SetupError("person_id_conflict", "Member ID already exists.")

        # SecretStore first: shared key metadata moves independently of local
        # value freshness, and a store failure leaves the member config
        # untouched so the same update can be retried. The values are read
        # before any key moves, so an unreadable key file changes nothing.
        account_info, secrets = _stored_accounts(config, config.original_person_id)
        pending_secrets = {new_prefix + name: value for name, value in secrets.items()}
        # An ambiguous source owns no keys. Repair only its config; explicitly
        # supplied secrets use the unique target.
        old = _owned_secrets(config.config_dir, config.original_person_id)
        if old.store or pending_secrets:
            store = old.store or resolve_secret_store(config.config_dir)
            renamed = False
            if old.store and old.prefix != new_prefix:
                indexed = set(store.keys())
                for name, old_key in old.keys().items():
                    if old_key in indexed:
                        store.rename(old_key, new_prefix + name)
                        renamed = True
            for key, value in pending_secrets.items():
                store.set(key, value)
            if renamed or pending_secrets:
                files.append(CreatedFile(path=store.location, action="update"))

        if config.original_person_id != config.person_id:
            old_person_dir.rename(new_person_dir)
            files.extend(
                _retarget_default_person_id(
                    config.config_dir, config.original_person_id, config.person_id
                )
            )

        person_file = new_person_dir / "person.yml"
        person_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(person_file, self.build_person_config(config, account_info))
        _saved(config)
        files.append(CreatedFile(path=person_file, action="update"))

        return PersonSetupResult(files=files)

    @shared_write_operation
    def delete_person(self, *, config_dir: Path, person_id: str) -> PersonSetupResult:
        files: list[CreatedFile] = []
        person_dir = stored_person_config_dir(config_dir, person_id)
        person_file = person_dir / "person.yml"

        # SecretStore first: indexed keys are removed whether or not this
        # device holds their current generation, and a store failure leaves
        # the member config intact so the deletion can be retried.
        owned = _owned_secrets(config_dir, person_id)
        if owned.store:
            indexed = sorted(
                set(owned.keys().values()).intersection(owned.store.keys())
            )
            for key in indexed:
                owned.store.delete(key)
            if indexed:
                files.append(CreatedFile(path=owned.store.location, action="update"))

        shutil.rmtree(person_dir)
        files.append(CreatedFile(path=person_file, action="delete"))
        files.extend(_retarget_default_person_id(config_dir, person_id, ""))
        return PersonSetupResult(files=files)

    def build_person_config(
        self,
        config: PersonSetupInput,
        account_info: Mapping[str, str],
        *,
        include_initial_defaults: bool = False,
    ) -> dict:
        """The member's config, with the ``account_info`` its accounts store."""
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
                "git_user": config.person_name,
                "git_email": config.git_email,
                **account_info,
            },
            "profile": profile,
            "speaking_style": "" if is_human else config.speaking_style,
            "relationships": "" if is_human else config.relationships,
        }
        if config.chat_channels and not is_human:
            chat_channel = _chat_channel(config.config_dir)
            person_config["message_channels"] = [
                _message_channel(
                    chat_channel(channel),
                    channel,
                    person_id=config.person_id,
                    participation=chat_participation(
                        config.chat_channel_participation.get(channel)
                    ),
                )
                for channel in config.chat_channels
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
            and not is_human
            and Service.TICKET_SERVICE.value in _project_services(config.config_dir)
        ):
            routine_commands = [TICKET_WORKFLOW_COMMAND]
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


def _stored_accounts(
    config: PersonSetupInput, saved_person_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    """The ``account_info`` entries and the secrets, by member-relative name,
    the member's accounts store; what was set up for the member (an app
    registration, for one) was set up under its saved ID.

    Raises:
        SetupError: If an account cannot be stored as the form names it.
    """
    account_info: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for account in config.accounts.values():
        info, values = account.stored(saved_person_id, config.person_type == "human")
        account_info |= info
        secrets |= values
    return account_info, secrets


def _saved(config: PersonSetupInput) -> None:
    for account in config.accounts.values():
        account.saved()


async def prepare_accounts(config: PersonSetupInput, saved_person_id: str) -> None:
    """Have each account obtain from its provider what saving the member
    needs, before the save takes the workspace's lock: no write waits on a
    provider. The member's credentials on each host are handed to every
    account.

    Raises:
        SetupError: If a provider refuses; nothing is saved.
    """
    account_info, secrets = _stored_accounts(config, saved_person_id)
    held = _owned_secrets(config.config_dir, saved_person_id)
    member = SavedMember(
        person_id=config.person_id,
        name=config.person_name,
        human=config.person_type == "human",
        account_info=account_info,
        get_secret=lambda name: secrets.get(name) or held.value(name),
        services=_services_of(config.config_dir),
        avatar=find_avatar_file(config.config_dir, config.person_id)
        if is_valid_person_id(config.person_id)
        else None,
    )
    member = replace(
        member,
        credentials={
            provider.name: credential
            for provider in PROVIDERS.values()
            if provider.setup
            and provider.setup.member
            and (credential := provider.setup.member.credential(member))
        },
    )
    for account in config.accounts.values():
        await account.prepare(member)


async def remove_accounts(config_dir: Path, person_id: str) -> None:
    """Have each provider undo what saving the member obtained from it,
    before the member is deleted.

    Raises:
        SetupError: If a provider refuses; the member is kept.
    """
    member = _saved_member(config_dir, person_id)
    for provider in PROVIDERS.values():
        if provider.setup and provider.setup.member:
            await provider.setup.member.removed(member)


async def share_avatar(config_dir: Path, person_id: str) -> None:
    """Hand the member's picture, which has just changed, to each provider
    its accounts are with.

    Raises:
        SetupError: If a provider refuses; the picture stays changed here.
    """
    member = _saved_member(config_dir, person_id)
    for provider in PROVIDERS.values():
        if provider.setup and provider.setup.member:
            await provider.setup.member.avatar_changed(member)


def _saved_member(config_dir: Path, person_id: str) -> SavedMember:
    """The saved member ``person_id``, as its accounts see it."""
    member_dir = stored_person_config_dir(config_dir, person_id)
    person_data = _read_member_form(member_dir / "person.yml")
    return SavedMember(
        person_id=person_id,
        name=str(person_data.get("name", "")),
        human=person_data.get("person_type") == "human",
        account_info={
            key: str(value)
            for key, value in _config_mapping(person_data.get("account_info")).items()
        },
        get_secret=_owned_secrets(config_dir, person_id).value,
        services=_services_of(config_dir),
        avatar=avatar_in(member_dir),
    )


def _services_of(config_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        kind: _config_mapping(item)
        for kind, item in _project_services(config_dir).items()
    }


def _message_channel(
    chat: dict[str, str], name: str, *, person_id: str, participation: str
) -> dict[str, Any]:
    return {
        "name": name,
        "used_as": ["internal_communication"],
        "used_by": [person_id],
        "channel_info": (
            {"channel_id": chat["channel_id"]} if "channel_id" in chat else {}
        ),
        "chat": {"enabled": True, "participation": participation, **chat},
    }
