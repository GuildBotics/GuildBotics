import os
from enum import Enum
from typing import Any, ClassVar

import langcodes
from langcodes import Language
from pydantic import BaseModel, Field, PrivateAttr

from guildbotics.entities.task import ScheduledTask, Task


class Service(Enum):
    """
    Enum representing different services used in the project.
    """

    FILE_STORAGE = "file_storage"
    TICKET_MANAGER = "ticket_manager"
    CODE_HOSTING_SERVICE = "code_hosting_service"


class Repository(BaseModel):
    """
    A class representing a repository.

    Attributes:
        name (str): The name of the repository.
        description (str): A brief description of the repository.
        is_default (bool): Indicates if this is the default repository.
    """

    name: str = Field(..., description="The name of the repository.")
    description: str = Field(
        default="", description="A brief description of the repository."
    )
    is_default: bool = Field(
        default=False, description="Indicates if this is the default repository."
    )

    def __str__(self):
        return f"Repository(name={self.name})"


class Project(BaseModel):
    """
    A class representing a project.

    Attributes:
        name (str): The name of the project.
        language (str): The default language for the project, represented as a language tag.
        repositories (list[Repository]): A list of repositories used in the project.
        services (dict[str, dict[str, str]]): A dictionary of services used in the project.
    """

    name: str = Field(default="", description="The name of the project.")
    language: str = Field(
        default="en",
        description="The default language for the project, represented as a language tag.",
    )
    repositories: list[Repository] = Field(
        default_factory=list,
        description="A list of repositories used in the project.",
    )
    # A dictionary of services used in the project, where keys are service names and values are
    services: dict[str, dict[str, str | dict[str, str]]] = Field(
        default_factory=dict,
        description="A dictionary of services used in the project.",
    )

    _language: Language | None = PrivateAttr(default=None)

    def get_default_repository(self) -> Repository:
        """
        Get the default repository for the project.

        Returns:
            Repository: The default repository object.
        """
        for repo in self.repositories:
            if repo.is_default:
                return repo

        if self.repositories:
            return self.repositories[0]
        else:
            raise ValueError("No default repository found in the project.")

    def __str__(self):
        return f"Project(name={self.name})"

    def get_service_config(self, service: Service) -> dict[str, str | dict[str, str]]:
        """
        Get the configuration for a specific service.
        Args:
            service (Service): The service enum to get the configuration for.
        Returns:
            dict[str, str]: The configuration dictionary for the specified service.
        """
        return self.services.get(service.value, {})

    def get_service_name(self, service: Service) -> str:
        """
        Get the name of the service from the team's project services.
        Args:
            service (Service): The service enum to get the name for.
            team (Team): The team associated with the service.
        Returns:
            str: The name of the service.
        """
        config = self.get_service_config(service)
        return str(config.get("name", "")).lower()

    def is_available_service(self, service: Service) -> bool:
        """
        Check if a specific service is available in the project.
        Args:
            service (Service): The service enum to check availability for.
        Returns:
            bool: True if the service is available, False otherwise.
        """
        return self.get_service_name(service) != ""

    def get_available_services(self) -> list[Service]:
        """
        Get a list of available services that can be created by this factory.
        Returns:
            list[Service]: A list of available services.
        """
        available_services = []
        if self.is_available_service(Service.FILE_STORAGE):
            available_services.append(Service.FILE_STORAGE)
        if self.is_available_service(Service.TICKET_MANAGER):
            available_services.append(Service.TICKET_MANAGER)
        if self.is_available_service(Service.CODE_HOSTING_SERVICE):
            available_services.append(Service.CODE_HOSTING_SERVICE)
        return available_services

    def _get_language(self) -> Language:
        if self._language is None:
            tag = "en"
            if self.language:
                tag = self.language

            try:
                lang = Language.get(tag)
            except langcodes.LanguageTagError:
                try:
                    lang = langcodes.find(tag)
                except Exception:
                    lang = Language.get("en")
            self._language = lang
        return self._language

    def get_language_code(self) -> str:
        """
        Get the language code for the project's default language.
        Returns:
            str: The language code for the project's default language.
        """
        return self._get_language().language or "en"

    def get_language_name(self) -> str:
        """
        Get the name of the project's default language.
        Returns:
            str: The name of the project's default language.
        """
        lang = self._get_language()
        return lang.display_name(lang.language or "en")


class TaskSchedule(BaseModel):
    """
    A class representing a routine task, including the task details and schedules.

    Attributes:
        id (Optional[int]): The unique identifier for the routine task.
        task (Task): The task details.
        schedules (List[str]): Cron schedule expressions for the routine task.
    """

    task: Task = Field(..., description="The task details.")
    schedules: list[str] = Field(
        ..., description="Cron schedule expressions for the routine task."
    )


class MessageChannel(BaseModel):
    """
    A class representing a message channel.

    Attributes:
        id (str): The unique identifier for the message channel.
        service (str): The service where the message channel is hosted (e.g., Discord, Slack).
        name (str): The name of the message channel.
        used_as (list[str]): A list of roles that use the message channel.
        used_by (list[str]): A list of roles that are used by the message channel.
        channel_info (dict): Additional information about the channel.
    """

    id: str = Field(..., description="The unique identifier for the message channel.")
    name: str = Field(..., description="The name of the message channel.")
    service: str = Field(
        ...,
        description="The service where the message channel is hosted (e.g., Discord, Slack).",
    )
    used_as: list[str] = Field(
        ..., description="A list of roles that use the message channel."
    )
    used_by: list[str] = Field(
        ..., description="A list of roles that are used by the message channel."
    )
    channel_info: dict = Field(
        default_factory=dict, description="Additional information about the channel."
    )


class Role(BaseModel):
    """
    A class representing a role within the team.

    Attributes:
        id (str): The unique identifier for the role.
        summary (str): A brief summary of the role.
        description (str): A brief description of the role.
    """

    id: str = Field(..., description="The unique identifier for the role.")
    summary: str = Field(..., description="A brief summary of the role.")
    description: str = Field(..., description="A brief description of the role.")

    def update_by(self, other: "Role") -> "Role":
        """
        Update the role with another role's attributes.

        Args:
            other (Role): The role to update from.
        """
        if not isinstance(other, Role):
            raise TypeError("Expected other to be an instance of Role.")
        self.summary = other.summary if other.summary else self.summary
        self.description = other.description if other.description else self.description
        return self

    def __str__(self):
        return f"{self.id}. {self.summary} {self.description}"


class Person(BaseModel):
    """
    A class representing a person of the team.

    Attributes:
        person_id (str): The unique identifier for the person.
        name (str): The name of the person.
        is_active (bool): Indicates if the person is currently active on the platform.
        person_type (str): The type of person (e.g., human, machine_user).
        roles (dict[str, Role]): A dictionary mapping role IDs to Role objects.
        account_info (dict[str, str]): A dictionary containing account information for the person.
        profile (dict): A dictionary containing the profile information for the person.
        task_schedules (list[TaskSchedule]): A list of routine tasks for the person.
        relationships (str): A dictionary describing relationships with other members.
        speaking_style (str): A dictionary describing speaking style per member.
        message_channels (list[MessageChannel]): A list of message channels the person uses.
    """

    DEFINED_ROLES: ClassVar[dict[str, Role]] = {}

    person_id: str = Field(..., description="The unique identifier for the person.")
    name: str = Field(..., description="The name of the person.")
    is_active: bool = Field(
        default=False,
        description="Indicates if the person is currently active on the platform.",
    )
    person_type: str = Field(
        default="",
        description="The type of person (e.g., human, machine_user).",
    )
    roles: dict[str, Role] = Field(
        default_factory=dict,
        description="A dictionary mapping role IDs to Role objects.",
    )
    account_info: dict[str, Any] = Field(
        default_factory=dict,
        description="A dictionary containing account information for the person.",
    )
    profile: dict = Field(
        default_factory=dict,
        description="A dictionary containing the profile information for the person.",
    )
    task_schedules: list[TaskSchedule] = Field(
        default_factory=list, description="A list of routine tasks for the person."
    )
    relationships: str = Field(
        default="",
        description="A dictionary describing relationships with other members.",
    )
    speaking_style: str = Field(
        default="", description="A dictionary describing speaking style per member."
    )
    message_channels: list[MessageChannel] = Field(
        default_factory=list, description="A list of message channels the person uses."
    )

    def __str__(self):
        return f"Person(person_id={self.person_id}, name={self.name})"

    def __eq__(self, other):
        """Check equality based on person_id."""
        if isinstance(other, Person):
            return self.person_id == other.person_id
        return False

    def __hash__(self):
        """Return hash based on person_id."""
        return hash(self.person_id)

    def get_scheduled_tasks(self) -> list[ScheduledTask]:
        """
        Get the scheduled tasks for the person.

        Returns:
            list[ScheduledTask]: A list of scheduled tasks for the person.
        """
        scheduled_tasks: list[ScheduledTask] = []
        for task_schedule in self.task_schedules:
            scheduled_tasks.extend(
                [
                    ScheduledTask(task=task_schedule.task, schedule=s)
                    for s in task_schedule.schedules
                ]
            )
        return scheduled_tasks

    def get_role(self, role_id: str | None) -> Role:
        """
        Get a role by its ID.

        Args:
            role_id (str): The ID of the role to retrieve.

        Returns:
            Role: The role object if found, otherwise raises KeyError.
        """
        if role_id is None:
            role_id = "professional"

        return self.roles.get(
            role_id,
            Person.DEFINED_ROLES.get(
                role_id, Role(id=role_id, summary="", description="")
            ),
        )

    def get_role_descriptions(
        self, role_ids: list[str] | None = None
    ) -> dict[str, str]:
        """
        Get a description of the person's roles.
        Args:
            role_ids (list[str] | None): A list of role IDs to filter the descriptions. If None, all roles are included.

        Returns:
            dict[str, str]: A dictionary mapping role IDs to their descriptions.
        """
        if role_ids is None:
            role_ids = list(self.roles.keys())
        return {
            role_id: role.description
            for role_id, role in self.roles.items()
            if role_id in role_ids
        }

    def to_person_env_key(self, key) -> str:
        sanitized_id = self.person_id.replace("-", "_")
        return f"{sanitized_id.upper()}_{key.upper()}"

    def get_secret(self, key: str) -> str:
        """
        Get a secret value from the environment variables with the person's ID.

        Args:
            key (str): The key of the secret to retrieve.

        Returns:
            str: The value of the secret if it exists, otherwise raises KeyError.
        """
        env_key = self.to_person_env_key(key)
        if env_key in os.environ:
            return os.environ[env_key]
        else:
            raise KeyError(
                f"Environment variable '{env_key}' is not set and no default value provided."
            )

    def has_secret(self, key: str) -> bool:
        """
        Check if a secret exists for the given key.

        Args:
            key (str): The key of the secret to check.

        Returns:
            bool: True if the secret exists, False otherwise.
        """
        env_key = self.to_person_env_key(key)
        return env_key in os.environ


class Team(BaseModel):
    """
    A class representing a team.

    Attributes:
        project (Project): The project associated with the team.
        members (list[Person]): A list of members in the team.
    """

    project: Project = Field(..., description="The project associated with the team.")
    members: list[Person] = Field(..., description="A list of members in the team.")

    def get_role_members(self) -> dict[str, list[Person]]:
        """
        Get a dictionary mapping role_ids to their members.

        Returns:
            dict[str, list[Person]]: A dictionary where keys are role IDs and values are lists of member objects.
        """
        role_members: dict[str, list[Person]] = {}
        for member in self.members:
            for role_id in member.roles.keys():
                if role_id not in role_members:
                    role_members[role_id] = []
                role_members[role_id].append(member)
        return role_members

    def get_available_role_ids(self) -> list[str]:
        """
        Get a list of all available roles in the team.

        Returns:
            list[str]: A list of role IDs representing the available roles.
        """
        return list(self.get_role_members().keys())
