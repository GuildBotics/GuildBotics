"""Registry of the per-kind validators for shared workspace files.

The synchronization machinery must validate every shared file twice -- once
before sending, and once on arrival -- yet it holds no knowledge of what any
file contains. This registry is how the two meet: each module that owns a kind
of shared file registers the validator for it, and the sync boundary looks the
validator up by path prefix.

A registry rather than direct imports is what keeps the dependency arrows
pointing the right way. ``guildbotics.observability`` may depend on ``utils``
alone and could never be imported by the storage layer, so the owner pushes its
validator down here instead of the boundary reaching up for it.
"""

from __future__ import annotations

from collections.abc import Callable

#: A validator receives the ``.guildbotics``-relative path and the file bytes.
SharedFileValidator = Callable[[str, bytes], None]


class SharedFileInvalidError(ValueError):
    """Raised when a shared file fails the validation its kind requires.

    Attributes:
        relative_path (str): The path relative to ``.guildbotics/``.
        reason (str): What the file failed, phrased to follow the path.
    """

    def __init__(self, relative_path: str, reason: str) -> None:
        super().__init__(f"{relative_path}: {reason}")
        self.relative_path = relative_path
        self.reason = reason


#: The shared kinds whose meaning is defined by an owning module, mapped to the
#: module that registers the validator for each.
#:
#: Registration happens when the owner is imported, so a process that
#: synchronizes without having loaded one would validate that kind by syntax
#: alone -- a boundary that quietly weakens depending on import order, in the
#: one place where "this file did not validate" is supposed to mean damage.
#: Declaring the kinds here turns a missing owner into a refusal instead: the
#: sending side holds the file back and the receiving side stops, rather than
#: passing content nobody checked.
OWNED_SHARED_PREFIXES = {
    "config/secrets.yml": "guildbotics.utils.secret_store",
    "state/chat_state": "guildbotics.integrations.file_chat_state_store",
    "state/documents": "guildbotics.capabilities.member_memory",
    "state/events": "guildbotics.observability.activity_event_store",
    "state/task-runs": "guildbotics.capabilities.task_runs",
}

_validators: dict[str, SharedFileValidator] = {}


def register_shared_validator(prefix: str, validator: SharedFileValidator) -> None:
    """Register the validator that owns every shared file under ``prefix``.

    Args:
        prefix (str): A ``.guildbotics``-relative directory prefix, for example
            ``state/events``.
        validator (SharedFileValidator): Raises
            :class:`SharedFileInvalidError` when a file does not satisfy its kind.
    """
    _validators[prefix.strip("/")] = validator


def find_shared_validator(relative_path: str) -> SharedFileValidator | None:
    """Return the validator owning ``relative_path``, most specific prefix first."""
    for prefix in _prefixes_of(relative_path):
        validator = _validators.get(prefix)
        if validator is not None:
            return validator
    return None


def require_shared_validator(relative_path: str) -> SharedFileValidator | None:
    """Return the validator owning ``relative_path``, refusing a missing owner.

    Returns:
        SharedFileValidator | None: The validator, or None for a kind that has
            no owner at all and is therefore held to its syntax alone.

    Raises:
        SharedFileInvalidError: When the kind is declared in
            :data:`OWNED_SHARED_PREFIXES` but its owner has not been imported,
            so validating the file would silently check less than it should.
    """
    validator = find_shared_validator(relative_path)
    if validator is not None:
        return validator
    for prefix in _prefixes_of(relative_path):
        owner = OWNED_SHARED_PREFIXES.get(prefix)
        if owner is not None:
            raise SharedFileInvalidError(
                relative_path,
                f"cannot be validated because {owner} is not loaded",
            )
    return None


def _prefixes_of(relative_path: str) -> list[str]:
    """Return the path's prefixes, most specific first."""
    parts = relative_path.strip("/").split("/")
    return ["/".join(parts[:depth]) for depth in range(len(parts), 0, -1)]
