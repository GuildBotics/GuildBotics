"""Member identifiers are directory components on every supported OS."""

import re
from pathlib import PureWindowsPath

PERSON_ID_REQUIREMENT = "person_id must contain only lowercase letters, digits, _ or - and must not be a Windows reserved name"


def is_valid_person_id(value: str) -> bool:
    return (
        re.fullmatch(r"[a-z0-9_-]+", value) is not None
        and not PureWindowsPath(value).is_reserved()
    )


def validate_person_id(value: str) -> str:
    if not is_valid_person_id(value):
        raise ValueError(PERSON_ID_REQUIREMENT)
    return value
