"""Validation of shared workspace files, by file kind.

One implementation serves two callers that must never disagree: the sending
side refuses to share a file it cannot validate, and the receiving side treats
a shared file it cannot validate as corruption rather than as a normal
concurrent update. Because only validated content is ever shared, a receiving
failure means a defect or a damaged repository, not a user mistake.

The synchronization machinery itself holds no knowledge of file contents. It
calls :func:`validate_shared_file`, which applies two layers. The boundary
itself enforces what holds for every shared file regardless of kind: it is
inside the shared roots, within its size bound, decodable, and syntactically
well-formed. On top of that, the implementation that owns the file kind adds
meaning -- a validator the owning module registered in
``guildbotics.utils.shared_file_validators``, the entity models for config, the
record models for workspace and device identity, or a kind check for member
avatars.

Keeping the two layers separate is deliberate. An owner never has to restate
the size bound or the encoding check to add a semantic rule, and a kind with no
owner yet is still held to the boundary, so nothing is silently exempted by
having been forgotten.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PurePosixPath

import yaml  # type: ignore
from pydantic import BaseModel, ValidationError

from guildbotics.entities import Person, Project
from guildbotics.utils.avatar import SUPPORTED_EXTENSIONS
from guildbotics.utils.shared_file_validators import (
    SharedFileInvalidError,
    find_shared_validator,
)
from guildbotics.utils.workspace_sync_port import SHARED_ROOTS
from guildbotics.workspace.identity import DeviceRecord, WorkspaceIdentity

__all__ = [
    "MAX_SHARED_AVATAR_BYTES",
    "MAX_SHARED_FILE_BYTES",
    "MAX_SHARED_JOURNAL_BYTES",
    "SharedFileInvalidError",
    "validate_shared_file",
]

#: Shared records stay small; bulk console and prompt data belongs in ``local/``.
MAX_SHARED_FILE_BYTES = 1_048_576
#: Append journals (``*.jsonl``) trade a larger bound for their own trimming.
MAX_SHARED_JOURNAL_BYTES = 8 * 1024 * 1024
#: Member avatars are the only binary the normal shared set carries.
MAX_SHARED_AVATAR_BYTES = 4_194_304


def validate_shared_file(relative_path: str, data: bytes) -> None:
    """Validate one shared file's bytes.

    Args:
        relative_path (str): The path relative to ``.guildbotics/``, for example
            ``state/devices/<device_id>.json``.
        data (bytes): The complete file content.

    Raises:
        SharedFileInvalidError: When the path is not shared, the file is too
            large, or the content does not satisfy its kind.
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
        # Append journals trade a larger bound for their own trimming.
        _require_size(relative_path, data, MAX_SHARED_JOURNAL_BYTES)
        _require_json_lines(relative_path, _require_utf8(relative_path, data))
    else:
        _require_size(relative_path, data, MAX_SHARED_FILE_BYTES)
        text = _require_utf8(relative_path, data)
        model = _record_model(path)
        if model is not None:
            _require_model(relative_path, model, text)
        elif path.suffix in {".yml", ".yaml"}:
            _require_yaml(relative_path, text)
        elif path.suffix == ".json":
            _require_json(relative_path, text)

    registered = find_shared_validator(relative_path)
    if registered is not None:
        registered(relative_path, data)


_MEMBERS_DIR = ("config", "team", "members")
_DEVICES_DIR = ("state", "devices")
_PROJECT_FILE = ("config", "team", "project.yml")
_WORKSPACE_FILE = ("state", "workspace.json")


def _record_model(path: PurePosixPath) -> type[BaseModel] | None:
    """Return the model that owns ``path``, or None when only syntax is defined."""
    if path.parts == _PROJECT_FILE:
        return Project
    if _is_member_file(path) and path.name == "person.yml":
        return Person
    if path.parts == _WORKSPACE_FILE:
        return WorkspaceIdentity
    if _is_child_of(path, _DEVICES_DIR):
        return DeviceRecord
    return None


def _is_child_of(path: PurePosixPath, directory: tuple[str, ...]) -> bool:
    """True when ``path`` names a file directly inside ``directory``."""
    parts = path.parts
    return parts[: len(directory)] == directory and len(parts) == len(directory) + 1


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


def _require_json_lines(relative_path: str, text: str) -> None:
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            raise SharedFileInvalidError(
                relative_path, f"has invalid JSON on line {number}: {exc}"
            ) from exc


def _require_model(relative_path: str, model: type[BaseModel], text: str) -> None:
    parse: Callable[[str], object] = (
        _require_json_for(relative_path)
        if relative_path.endswith(".json")
        else _require_yaml_for(relative_path)
    )
    try:
        model.model_validate(parse(text))
    except ValidationError as exc:
        raise SharedFileInvalidError(
            relative_path,
            f"does not match {model.__name__}: {exc.error_count()} errors",
        ) from exc


def _require_json_for(relative_path: str) -> Callable[[str], object]:
    return lambda text: _require_json(relative_path, text)


def _require_yaml_for(relative_path: str) -> Callable[[str], object]:
    return lambda text: _require_yaml(relative_path, text)
