"""Member identifiers are directory components on every supported OS."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, ValidationError

_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{kind}{number}" for kind in ("com", "lpt") for number in range(1, 10)
}


def is_valid_person_id(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9_-]+", value) is not None and value not in _RESERVED


def validate_person_id(value: str) -> str:
    if not is_valid_person_id(value):
        from guildbotics.utils.i18n_tool import t

        raise ValueError(t("member_config.id_requirement"))
    return value


PersonId = Annotated[str, AfterValidator(validate_person_id)]


def validate_optional_person_id(value: str) -> str:
    return validate_person_id(value) if value else value


OptionalPersonId = Annotated[str, AfterValidator(validate_optional_person_id)]

PERSON_SECRET_ENV_SUFFIXES = (
    "GITHUB_ACCESS_TOKEN",
    "GITHUB_PRIVATE_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
)


def person_env_prefix(person_id: str) -> str:
    """Return the shared secret and environment namespace for a stored ID."""
    return person_id.replace("-", "_").upper()


def member_env_prefix_groups(members: Path) -> dict[str, list[Path]]:
    """Group all stored members, including repairable configs, by namespace."""
    groups: dict[str, list[Path]] = {}
    for directory in iter_member_config_directories(members):
        groups.setdefault(person_env_prefix(directory.name), []).append(directory)
    return groups


def person_env_conflicts(
    members: Path, person_id: str, *, exclude: str | None = None
) -> list[Path]:
    """Find other owners of a namespace, without reading member data or secrets."""
    return [
        directory
        for directory in member_env_prefix_groups(members).get(
            person_env_prefix(person_id), []
        )
        if directory.name not in {person_id, exclude}
    ]


def validate_person_env_prefix(
    members: Path, person_id: str, *, exclude: str | None = None
) -> None:
    """Refuse to use an ambiguous namespace and identify its config files."""
    if conflicts := person_env_conflicts(members, person_id, exclude=exclude):
        from guildbotics.utils.i18n_tool import t

        raise MemberConfigError(
            members / person_id / "person.yml",
            ValueError(
                t(
                    "member_config.prefix_conflict",
                    members=", ".join(str(path / "person.yml") for path in conflicts),
                )
            ),
        )


def ambiguous_person_env_keys(members: Path) -> frozenset[str]:
    """Keys whose stored member owner cannot be identified uniquely."""
    return frozenset(
        f"{prefix}_{suffix}"
        for prefix, directories in member_env_prefix_groups(members).items()
        if len(directories) > 1
        for suffix in PERSON_SECRET_ENV_SUFFIXES
    )


def validate_member_directory_name(value: str) -> str:
    """Address stored configuration for repair, without authorizing a member."""
    if (
        value in {"", ".", ".."}
        or not value.isprintable()
        or any(character in value for character in "/\\:")
        or value.endswith((".", " "))
    ):
        from guildbotics.utils.i18n_tool import t

        raise ValueError(t("member_config.directory_name"))
    return value


MemberDirectoryName = Annotated[str, AfterValidator(validate_member_directory_name)]


def stored_person_config_directory(name: str) -> Path:
    return Path("team/members") / validate_member_directory_name(name)


def person_config_directory(person_id: str) -> Path:
    """The same validated relative member directory for revisions and IO."""
    return stored_person_config_directory(validate_person_id(person_id))


def iter_member_config_directories(members: Path) -> Iterator[Path]:
    """A member is a child directory containing a configuration file."""
    if members.is_dir():
        for child in sorted(members.iterdir()):
            if child.is_dir() and (child / "person.yml").is_file():
                yield child


class MemberConfigError(ValueError):
    """An invalid member file, addressable in the configuration editor."""

    def __init__(self, path: Path, error: Exception) -> None:
        from guildbotics.utils.i18n_tool import t

        self.filename = str(path)
        reason = (
            t(
                "member_config.invalid_fields",
                fields=", ".join(
                    ".".join(map(str, detail["loc"])) or "person.yml"
                    for detail in error.errors()
                ),
            )
            if isinstance(error, ValidationError)
            else str(error)
        )
        super().__init__(t("member_config.invalid", path=path, reason=reason))
