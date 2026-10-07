from pathlib import Path

from yaml import YAMLError

from guildbotics.entities import Person, Project, Team
from guildbotics.loader import TeamLoader
from guildbotics.loader.yaml.yaml_role_loader import YamlRoleLoader
from guildbotics.utils.fileio import get_config_path, load_yaml_file
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.person_id import (
    MemberConfigError,
    iter_member_config_directories,
    member_env_prefix_groups,
    validate_member_directory_name,
    validate_person_env_prefix,
)


class YamlTeamLoader(TeamLoader):
    def __init__(self, dir: str | None = None):
        """
        Initialize the YamlTeamLoader with a directory path.
        Args:
            dir (str | None): The directory path where team YAML files are stored.
            If None, defaults to the storage path for teams.
        """
        super().__init__()
        self.dir = get_config_path("team") if dir is None else Path(dir)

    def load(self) -> Team:
        """
        Load the team from YAML files.
        Returns:
            Team: The Team object.
        """
        project_data = load_yaml_file(self.dir / "project.yml")
        project = Project.model_validate(project_data)

        members: list[Person] = []
        members_dir = self.dir / "members"
        for directories in member_env_prefix_groups(members_dir).values():
            if len(directories) > 1:
                validate_person_env_prefix(members_dir, directories[0].name)
        if members_dir.exists():
            role_loader = YamlRoleLoader(project.get_language_code())
            Person.DEFINED_ROLES = role_loader.load_all()
            for d in iter_member_config_directories(members_dir):
                try:
                    validate_member_directory_name(d.name)
                    person_data = load_yaml_file(d / "person.yml")
                    person = Person.model_validate(person_data)
                except YAMLError as exc:
                    raise MemberConfigError(
                        d / "person.yml", ValueError(t("member_config.invalid_yaml"))
                    ) from exc
                except ValueError as exc:
                    raise MemberConfigError(d / "person.yml", exc) from exc
                if person.person_id != d.name:
                    raise MemberConfigError(
                        d / "person.yml",
                        ValueError(t("member_config.directory_mismatch")),
                    )
                role_loader.extract_roles_from_profile(person)

                members.append(person)

        return Team(members=members, project=project)
