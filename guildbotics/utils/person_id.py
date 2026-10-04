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
