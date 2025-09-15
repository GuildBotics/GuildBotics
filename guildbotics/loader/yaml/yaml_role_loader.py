from pathlib import Path

from guildbotics.entities import Person, Role
from guildbotics.utils.fileio import get_config_path, load_yaml_file


class YamlRoleLoader:

    def __init__(self, language_code: str | None = None):
        """
        Initialize the YamlRoleLoader with an optional language code.
        Args:
            language_code (str | None): The language code for the roles. If None, defaults
            to the default language.
        """
        self.language_code = language_code

    def load_all(self) -> dict[str, Role]:
        """
        Load the roles from YAML files.
        Returns:
            dict[str, Role]: A dictionary of roles.
        """

        roles_data: dict[str, Role] = {}

        # Load roles from the default template directory
        self._add_roles(
            get_config_path("roles/default.yml", self.language_code), roles_data
        )

        # Load roles from the storage path
        self._add_roles_from_dir(get_config_path("roles"), roles_data)

        return roles_data

    def extract_roles_from_profile(self, person: Person):
        """
        Load the roles for a person based on their skilled roles and profile.
        Args:
            person (Person): The person for whom to load roles.
        Returns:
            dict[str, Role]: A dictionary of roles for the person.
        """
        base_roles = self.load_all()
        self._update_roles_from_map(person.profile, base_roles, person.roles)

    def _add_roles_from_dir(self, dir: Path, roles_data: dict[str, Role]) -> None:
        if not dir.exists() or not dir.is_dir():
            return

        # Load roles from the specified directory
        for role_file in dir.glob(f"*.{self.language_code}.yml"):
            if role_file.name.startswith("default."):
                continue
            self._add_roles(role_file, roles_data)

    def _add_roles(self, path: Path, roles: dict[str, Role]) -> None:
        """
        Load roles from a YAML file and update the roles dictionary.
        Args:
            path (Path): The path to the YAML file containing roles.
            roles (dict[str, Role]): The dictionary to update with loaded roles.
        """
        data = load_yaml_file(path)
        if not isinstance(data, dict):
            raise TypeError("Expected data to be a dictionary.")
        role_map = data.get("roles", [])
        self._update_roles_from_map(role_map, roles, roles)

    def _update_roles_from_map(
        self,
        role_data: dict,
        base_roles: dict[str, Role],
        target_roles: dict[str, Role],
    ) -> None:
        """
        Update the target_roles dictionary with roles from the given role_data.
        Args:
            role_data (dict): A dictionary containing role data.
            base_roles (dict[str, Role]): The base roles to update from.
            target_roles (dict[str, Role]): The dictionary to update with loaded roles.
        """
        for role_id, role_info in role_data.items():
            if not isinstance(role_info, dict):
                role_info = {}
            new_role = Role(
                id=role_id,
                summary=role_info.get("summary", ""),
                description=role_info.get("description", ""),
            )
            if role_id in base_roles:
                target_roles[role_id] = base_roles[role_id].update_by(new_role)
            else:
                target_roles[role_id] = new_role
