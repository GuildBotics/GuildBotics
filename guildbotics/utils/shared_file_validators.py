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
    parts = relative_path.strip("/").split("/")
    for depth in range(len(parts), 0, -1):
        validator = _validators.get("/".join(parts[:depth]))
        if validator is not None:
            return validator
    return None
