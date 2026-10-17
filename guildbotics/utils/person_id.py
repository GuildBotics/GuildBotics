"""Member identifiers are directory components on every supported OS."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, ValidationError

from guildbotics.utils.fileio import get_identity_config_dir

#: A member's identity, under the identity root's ``team/members/<person_id>/``:
#: who the member is, whichever workspace it works in.
IDENTITY_FILE = "identity.yml"
#: A member's participation in a workspace, under that workspace's
#: ``team/members/<person_id>/``: what it does there.
MEMBER_FILE = "member.yml"

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


def identity_members_dir() -> Path:
    """The directory holding every member's identity."""
    return get_identity_config_dir() / "team/members"


def _namespaces() -> dict[str, list[Path]]:
    """Group all stored identities, including repairable ones, by namespace.

    Every member's secrets live in the one identity store, so the namespace is
    the identities', not any workspace's: two members that never share a
    workspace still share the store.
    """
    groups: dict[str, list[Path]] = {}
    for directory in iter_member_config_directories(
        identity_members_dir(), IDENTITY_FILE
    ):
        groups.setdefault(_prefix(directory.name), []).append(directory)
    return groups


def _prefix(name: str) -> str:
    return name.replace("-", "_").upper() + "_"


def person_secret_prefix(person_id: str) -> str:
    """The only way to a member's secret keys: the prefix of those it alone
    owns, which a member-relative name such as ``SLACK_BOT_TOKEN`` follows.

    Every stored identity counts, including those awaiting repair, and the
    directory is read on every call, so a collision that appears later
    withholds the keys from then on.

    Raises:
        PersonEnvPrefixConflictError: Another stored member shares the keys.
    """
    prefix = _prefix(person_id)
    if conflicts := [
        directory
        for directory in _namespaces().get(prefix, [])
        if directory.name != person_id
    ]:
        raise PersonEnvPrefixConflictError(
            identity_members_dir() / person_id, conflicts
        )
    return prefix


def is_member_secret(key: str) -> bool:
    """Whether ``key`` is a member's secret: one under a stored identity's
    prefix, which belongs in the identities' store alone."""
    return any(key.startswith(prefix) for prefix in _namespaces())


def ambiguous_person_secret() -> Callable[[str], bool]:
    """Whether a key belongs to a member whose owner cannot be identified
    uniquely: the member of the longest prefix the key has is shared."""
    namespaces = sorted(_namespaces().items(), key=lambda item: -len(item[0]))

    def ambiguous(key: str) -> bool:
        return next(
            (len(dirs) > 1 for prefix, dirs in namespaces if key.startswith(prefix)),
            False,
        )

    return ambiguous


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


def iter_member_config_directories(members: Path, filename: str) -> Iterator[Path]:
    """A member is a child directory containing ``filename``: an identity
    (:data:`IDENTITY_FILE`), or a workspace's participation (:data:`MEMBER_FILE`).
    """
    if members.is_dir():
        for child in sorted(members.iterdir()):
            if child.is_dir() and (child / filename).is_file():
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
                    ".".join(map(str, detail["loc"])) or path.name
                    for detail in error.errors()
                ),
            )
            if isinstance(error, ValidationError)
            else str(error)
        )
        super().__init__(t("member_config.invalid", path=path, reason=reason))


class PersonEnvPrefixConflictError(MemberConfigError):
    """Other stored members share a member's secret keys."""

    def __init__(self, directory: Path, conflicts: list[Path]) -> None:
        from guildbotics.utils.i18n_tool import t

        self.conflicts = conflicts
        super().__init__(
            directory / IDENTITY_FILE,
            ValueError(
                t(
                    "member_config.prefix_conflict",
                    members=", ".join(str(path / IDENTITY_FILE) for path in conflicts),
                )
            ),
        )
