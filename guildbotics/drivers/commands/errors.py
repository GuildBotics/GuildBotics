from __future__ import annotations

from typing import Sequence


class CustomCommandError(RuntimeError):
    """Base error raised when a custom command cannot be executed."""


class PersonSelectionRequiredError(CustomCommandError):
    """Raised when no person could be inferred for a command."""

    def __init__(self, available: Sequence[str]):
        super().__init__("Person selection required.")
        self.available = list(available)


class PersonNotFoundError(CustomCommandError):
    """Raised when the requested person is not part of the team."""

    def __init__(self, identifier: str, available: Sequence[str]):
        super().__init__(f"Person '{identifier}' not found.")
        self.identifier = identifier
        self.available = list(available)
