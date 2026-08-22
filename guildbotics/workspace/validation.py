"""What a file must satisfy to be shared between the user's machines.

One implementation serves two callers that must never disagree: the sending
side refuses to share a file it cannot validate, and the receiving side treats
a shared file it cannot validate as damage rather than as a normal concurrent
update.

What that check contains follows from what it can actually catch. Every shared
record is written by GuildBotics itself, from a shape its own code decides, so
re-checking those fields here would only restate what the writer already
guarantees -- a job for that writer's tests, not for a check on every
synchronization cycle. Files a person authors fail loudly through the product's
own paths on every device, so refusing to carry a half-finished edit would only
stop them continuing on another machine.

Three things survive that argument, and they are all this module does:

1. **The shared boundary itself** -- inside ``config/`` or ``state/``, within
   its size bound, decodable, and syntactically well-formed. Size is one of the
   two guarantees synchronization owes: the history must not grow without bound.
2. **A record written by a newer build.** A device running an older build
   cannot read a ``schema_version`` it does not implement, and no writer or
   local test can catch that -- only the device receiving it can.
3. **The secret key index**, which may name keys but has nowhere to put a
   value. That is the other guarantee synchronization owes: secret values must
   never enter the durable shared history.

``config/secrets.yml`` is the one shared record that cannot carry a
``schema_version``: rule 3 admits only ``store_id`` and ``keys`` at the top
level, so stamping it would make this boundary reject the file. That is the
right trade -- a field that has nowhere to go is how secret values are kept out
structurally -- but it means the generation check does not cover this one file,
and a change to its shape has to be made compatible on its own terms.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

import yaml  # type: ignore

from guildbotics.utils.avatar import SUPPORTED_EXTENSIONS
from guildbotics.utils.secret_store import SECRETS_INDEX_FILENAME
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    SHARED_ROOTS,
)

__all__ = [
    "MAX_SHARED_AVATAR_BYTES",
    "MAX_SHARED_FILE_BYTES",
    "MAX_SHARED_JOURNAL_BYTES",
    "SharedFileInvalidError",
    "SharedSchemaAheadError",
    "validate_shared_file",
]

#: Shared records stay small; bulk console and prompt data belongs in ``local/``.
MAX_SHARED_FILE_BYTES = 1_048_576
#: Append journals (``*.jsonl``) trade a larger bound for their own trimming.
MAX_SHARED_JOURNAL_BYTES = 8 * 1024 * 1024
#: Member avatars are the only binary the normal shared set carries.
MAX_SHARED_AVATAR_BYTES = 4_194_304

#: Everything the secret key index may say about a key. A value has no slot
#: here, which keeps secrets out of the shared history structurally rather than
#: by inspecting content for things that look like secrets.
SECRET_INDEX_ENTRY_FIELDS = frozenset({"generation", "updated_at"})

_SECRETS_INDEX_PATH = f"config/{SECRETS_INDEX_FILENAME}"


class SharedFileInvalidError(ValueError):
    """Raised when a shared file fails what the shared boundary requires.

    Attributes:
        relative_path (str): The path relative to ``.guildbotics/``.
        reason (str): What the file failed, phrased to follow the path.
    """

    def __init__(self, relative_path: str, reason: str) -> None:
        super().__init__(f"{relative_path}: {reason}")
        self.relative_path = relative_path
        self.reason = reason


class SharedSchemaAheadError(SharedFileInvalidError):
    """Raised when a record was written by a build newer than this one.

    Distinct from other failures because the answer is different: the shared
    data is fine and this device is behind, so the user updates GuildBotics
    rather than repairing anything.
    """

    def __init__(self, relative_path: str, version: int) -> None:
        super().__init__(
            relative_path,
            f"has schema_version {version}, which this build does not implement",
        )
        self.version = version


def validate_shared_file(relative_path: str, data: bytes) -> None:
    """Validate one shared file's bytes.

    Args:
        relative_path (str): The path relative to ``.guildbotics/``, for example
            ``state/devices/<device_id>.json``.
        data (bytes): The complete file content.

    Raises:
        SharedSchemaAheadError: When a record declares a newer schema version.
        SharedFileInvalidError: When the path is not shared, the file is too
            large, it is not decodable or well-formed, or the secret key index
            carries something other than key names and generations.
    """
    path = PurePosixPath(relative_path)
    if not path.parts or path.parts[0] not in SHARED_ROOTS:
        raise SharedFileInvalidError(
            relative_path, "is outside the shared config/ and state/ directories"
        )
    if _is_member_avatar(path):
        _validate_member_avatar(relative_path, path, data)
        return

    if path.suffix == ".jsonl":
        _require_size(relative_path, data, MAX_SHARED_JOURNAL_BYTES)
        _validate_json_lines(relative_path, _require_utf8(relative_path, data))
        return

    _require_size(relative_path, data, MAX_SHARED_FILE_BYTES)
    text = _require_utf8(relative_path, data)
    if path.suffix in {".yml", ".yaml"}:
        payload = _require_yaml(relative_path, text)
    elif path.suffix == ".json":
        payload = _require_json(relative_path, text)
    else:
        return
    _require_known_schema(relative_path, payload)
    if relative_path == _SECRETS_INDEX_PATH:
        _validate_secret_index(relative_path, payload)


def _require_known_schema(relative_path: str, payload: object) -> None:
    """Refuse a record this build is too old to read.

    Every shared record carries one ``schema_version`` generation, switched for
    all of them at once, so this one rule covers every kind without the
    boundary knowing what any of them contain.
    """
    if not isinstance(payload, dict):
        return
    version = payload.get("schema_version")
    if isinstance(version, int) and version > SHARED_RECORD_SCHEMA_VERSION:
        raise SharedSchemaAheadError(relative_path, version)


def _validate_secret_index(relative_path: str, payload: object) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise SharedFileInvalidError(relative_path, "is not a secret key index")
    unknown = sorted(set(map(str, payload)) - {"store_id", "keys"})
    if unknown:
        raise SharedFileInvalidError(relative_path, f"carries {', '.join(unknown)}")
    keys = payload.get("keys") or {}
    if not isinstance(keys, dict):
        raise SharedFileInvalidError(relative_path, "has a non-mapping key index")
    for key, entry in keys.items():
        if not isinstance(entry, dict):
            raise SharedFileInvalidError(relative_path, f"stores a value for {key}")
        extra = sorted(set(map(str, entry)) - SECRET_INDEX_ENTRY_FIELDS)
        if extra:
            raise SharedFileInvalidError(
                relative_path, f"records {', '.join(extra)} for {key}"
            )
        if not isinstance(entry.get("generation"), int):
            raise SharedFileInvalidError(
                relative_path, f"has a non-numeric generation for {key}"
            )


_MEMBERS_DIR = ("config", "team", "members")


def _is_member_file(path: PurePosixPath) -> bool:
    """True when ``path`` names a file inside ``config/team/members/<person_id>/``."""
    parts = path.parts
    return (
        parts[: len(_MEMBERS_DIR)] == _MEMBERS_DIR
        and len(parts) == len(_MEMBERS_DIR) + 2
    )


def _is_member_avatar(path: PurePosixPath) -> bool:
    return _is_member_file(path) and path.stem == "avatar"


def _validate_member_avatar(
    relative_path: str, path: PurePosixPath, data: bytes
) -> None:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise SharedFileInvalidError(
            relative_path, f"is not a supported avatar image ({path.suffix})"
        )
    _require_size(relative_path, data, MAX_SHARED_AVATAR_BYTES)


def _require_size(relative_path: str, data: bytes, limit: int) -> None:
    if len(data) > limit:
        raise SharedFileInvalidError(
            relative_path, f"is {len(data)} bytes, above the {limit} byte limit"
        )


def _require_utf8(relative_path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SharedFileInvalidError(relative_path, "is not valid UTF-8") from exc


def _require_yaml(relative_path: str, text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SharedFileInvalidError(
            relative_path, f"is not valid YAML: {exc}"
        ) from exc


def _require_json(relative_path: str, text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SharedFileInvalidError(
            relative_path, f"is not valid JSON: {exc}"
        ) from exc


def _validate_json_lines(relative_path: str, text: str) -> None:
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SharedFileInvalidError(
                relative_path, f"has invalid JSON on line {number}: {exc}"
            ) from exc
        _require_known_schema(relative_path, payload)
