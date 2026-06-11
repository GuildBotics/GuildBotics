from pathlib import Path

from guildbotics.entities import Person, Project, Team
from guildbotics.loader import TeamLoader
from guildbotics.loader.yaml.yaml_role_loader import YamlRoleLoader
from guildbotics.utils.fileio import get_config_path, load_yaml_file


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
                person = Person.model_validate(person_data)
                role_loader.extract_roles_from_profile(person)

                members.append(person)

        return Team(members=members, project=project)
