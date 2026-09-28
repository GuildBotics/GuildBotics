from __future__ import annotations

from collections.abc import Sequence


class CommandError(RuntimeError):
    """Base error raised when a custom command cannot be executed."""


class CommandFailedError(RuntimeError):
    """A command failed in its isolated environment otherwise than as a
    command: with an error of its code's own, of the type ``error_type``."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class PersonSelectionRequiredError(CommandError):
    """Raised when no person could be inferred for a command."""

    def __init__(self, available: Sequence[str]):
        super().__init__("Person selection required.")
        self.available = list(available)


class PersonNotFoundError(CommandError):
    """Raised when the requested person is not part of the team."""

    def __init__(self, identifier: str, available: Sequence[str]):
        super().__init__(f"Person '{identifier}' not found.")
        self.identifier = identifier
        self.available = list(available)


class PersonExecutionNotAllowedError(CommandError):
    """Raised when a member cannot be used as an AI execution subject."""

    def __init__(self, person_id: str):
        super().__init__(
            f"Human member '{person_id}' cannot be used as an AI execution subject."
        )
        self.person_id = person_id
