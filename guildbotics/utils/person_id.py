"""Member identifiers are directory components on every supported OS."""

import re
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator

PERSON_ID_REQUIREMENT = "person_id must contain only lowercase letters, digits, _ or - and must not be a Windows reserved name"
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{kind}{number}" for kind in ("com", "lpt") for number in range(1, 10)
}


def is_valid_person_id(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9_-]+", value) is not None and value not in _RESERVED


def validate_person_id(value: str) -> str:
    if not is_valid_person_id(value):
        raise ValueError(PERSON_ID_REQUIREMENT)
    return value


PersonId = Annotated[str, AfterValidator(validate_person_id)]


def validate_optional_person_id(value: str) -> str:
    return validate_person_id(value) if value else value


OptionalPersonId = Annotated[str, AfterValidator(validate_optional_person_id)]


def validate_member_directory_name(value: str) -> str:
    """Address stored configuration for repair, without authorizing a member."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("Member configuration directory must be one safe name")
    return value


MemberDirectoryName = Annotated[str, AfterValidator(validate_member_directory_name)]


def stored_person_config_directory(name: str) -> Path:
    return Path("team/members") / validate_member_directory_name(name)


def person_config_directory(person_id: str) -> Path:
    """The same validated relative member directory for revisions and IO."""
    return stored_person_config_directory(validate_person_id(person_id))


class MemberConfigError(ValueError):
    """An invalid member file, addressable in the configuration editor."""

    def __init__(self, path: Path, error: Exception) -> None:
        self.filename = str(path)
        super().__init__(f"Invalid member configuration {path}: {error}")
