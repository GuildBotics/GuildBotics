from pathlib import Path
from typing import Any

from yaml import YAMLError

from guildbotics.entities import Person, Project, Team
from guildbotics.entities.team import IDENTITY_FIELDS, PARTICIPATION_FIELDS
from guildbotics.loader.yaml.yaml_role_loader import YamlRoleLoader
from guildbotics.utils.fileio import get_config_path, load_yaml_file
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.person_id import (
    IDENTITY_FILE,
    MEMBER_FILE,
    MemberConfigError,
    identity_members_dir,
    iter_member_config_directories,
    person_secret_prefix,
    validate_member_directory_name,
)


class YamlTeamLoader:
    def __init__(self, dir: str | None = None):
        """
        Initialize the YamlTeamLoader with a directory path.
        Args:
            dir (str | None): The directory path where team YAML files are stored.
            If None, defaults to the storage path for teams.
        """
        self.dir = get_config_path("team") if dir is None else Path(dir)

    def load(self) -> Team:
        """
        Load the team from YAML files.

        A member of the team is one whose participation this workspace holds,
        joined with the identity it has in every workspace; one whose identity
        this machine does not have is left out.

        Returns:
            Team: The Team object.
        """
        project_data = load_yaml_file(self.dir / "project.yml")
        project = Project.model_validate(project_data)

        members: list[Person] = []
        members_dir = self.dir / "members"
        participants = list(iter_member_config_directories(members_dir, MEMBER_FILE))
        # Refuse any collision before reporting a config of its members invalid.
        for d in participants:
            person_secret_prefix(d.name)
        if participants:
            role_loader = YamlRoleLoader(project.get_language_code())
            Person.DEFINED_ROLES = role_loader.load_all()
        for d in participants:
            identity_file = identity_members_dir() / d.name / IDENTITY_FILE
            if not identity_file.is_file():
                # The identities arrive by a queue of their own, so a member
                # added on another machine can be here before its identity is,
                # and one deleted there stays here until it is taken out. Only
                # that member waits; the rest of the team works.
                get_logger().warning(
                    "Member %s has no identity on this machine yet; it is left "
                    "out of the team.",
                    d.name,
                )
                continue
            try:
                validate_member_directory_name(d.name)
                identity = _member_file(identity_file, IDENTITY_FIELDS)
                participation = _member_file(d / MEMBER_FILE, PARTICIPATION_FIELDS)
                person = Person.model_validate(
                    {
                        **identity,
                        **participation,
                        "profile": {
                            **identity.get("profile", {}),
                            **participation.get("profile", {}),
                        },
                    }
                )
            except ValueError as exc:
                if isinstance(exc, MemberConfigError):
                    raise
                raise MemberConfigError(d / MEMBER_FILE, exc) from exc
            if person.person_id != d.name:
                raise MemberConfigError(
                    identity_file,
                    ValueError(t("member_config.directory_mismatch")),
                )
            role_loader.extract_roles_from_profile(person)

            members.append(person)

        return Team(members=members, project=project)


def _member_file(path: Path, fields: frozenset[str]) -> dict[str, Any]:
    """One of a member's two files, holding only the fields it is for.

    A field in the other file's place is refused rather than read: an identity
    field in a workspace's file would make that workspace's member someone the
    others do not see.

    Raises:
        MemberConfigError: When the file is not a mapping of its own fields.
    """
    try:
        data = load_yaml_file(path)
    except YAMLError as exc:
        raise MemberConfigError(
            path, ValueError(t("member_config.invalid_yaml"))
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("profile", {}), dict):
        raise MemberConfigError(path, ValueError(t("member_config.invalid_yaml")))
    if misplaced := sorted(set(data) - fields):
        raise MemberConfigError(
            path,
            ValueError(t("member_config.invalid_fields", fields=", ".join(misplaced))),
        )
    return data
