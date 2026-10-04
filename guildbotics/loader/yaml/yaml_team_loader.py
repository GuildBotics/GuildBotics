from pathlib import Path

from pydantic import ValidationError

from guildbotics.entities import Person, Project, Team
from guildbotics.loader import TeamLoader
from guildbotics.loader.yaml.yaml_role_loader import YamlRoleLoader
from guildbotics.utils.fileio import get_config_path, load_yaml_file
from guildbotics.utils.person_id import MemberConfigError


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
        if members_dir.exists():
            role_loader = YamlRoleLoader(project.get_language_code())
            Person.DEFINED_ROLES = role_loader.load_all()
            for d in members_dir.iterdir():
                if not d.is_dir():
                    continue

                person_data = load_yaml_file(d / "person.yml")
                try:
                    person = Person.model_validate(person_data)
                except ValidationError as exc:
                    raise MemberConfigError(d / "person.yml", exc) from exc
                if person.person_id != d.name:
                    raise MemberConfigError(
                        d / "person.yml",
                        ValueError("person_id must match its directory name"),
                    )
                role_loader.extract_roles_from_profile(person)

                members.append(person)

        return Team(members=members, project=project)
