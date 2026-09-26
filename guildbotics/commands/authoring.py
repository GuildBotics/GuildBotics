"""What the command-authoring assistant returns, and how a proposal is checked."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guildbotics.commands.formats import EXTENSION_BY_FORMAT, CommandFormat
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_generated_command_source,
)

CommandAuthoringMode = Literal["create", "edit"]
CommandAuthoringFormat = CommandFormat
CommandAuthoringAction = Literal["answer", "propose_changes"]
CommandAuthoringOperation = Literal["create", "update"]


class CommandAuthoringChange(BaseModel):
    """One command source creation or update proposed by the assistant."""

    model_config = ConfigDict(extra="forbid")

    operation: CommandAuthoringOperation
    command: str = Field(min_length=1)
    format: CommandAuthoringFormat
    content: str = Field(min_length=1)


class CommandAuthoringResult(BaseModel):
    """One answer or explicit multi-command change proposal."""

    model_config = ConfigDict(extra="forbid")

    action: CommandAuthoringAction
    message: str
    changes: list[CommandAuthoringChange] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_action(self) -> CommandAuthoringResult:
        """Keep conversational answers separate from source changes."""
        if self.action == "answer" and self.changes:
            raise ValueError("An answer cannot include command changes.")
        if self.action == "propose_changes" and not self.changes:
            raise ValueError("A change proposal must include at least one change.")
        return self


def validate_proposal(
    result: CommandAuthoringResult,
    mode: CommandAuthoringMode,
    command: str,
    command_format: CommandAuthoringFormat | None,
    current_content: str,
) -> None:
    """Validate proposal scope and every generated command source.

    Args:
        result: The agent's change proposal.
        mode: Whether the request was a new command or an existing one.
        command: Current logical shared command name, or empty on creation.
        command_format: Current command format, or ``None`` on creation.
        current_content: Complete current editor buffer.

    Raises:
        CommandValidationError: If the proposal leaves its permitted scope,
            changes nothing, or holds an invalid command source.
    """
    seen: set[str] = set()
    has_effective_change = False
    for change in result.changes:
        if change.command in seen:
            raise CommandValidationError(
                "command_file_invalid_source",
                f"The proposal changes command '{change.command}' more than once.",
            )
        seen.add(change.command)
        if change.operation == "update":
            if (
                mode != "edit"
                or change.command != command
                or change.format != command_format
            ):
                raise CommandValidationError(
                    "command_file_invalid_source",
                    "A proposal may update only the currently edited command without changing its format.",
                )
            has_effective_change = (
                has_effective_change or change.content != current_content
            )
        else:
            if mode == "edit" and change.command == command:
                raise CommandValidationError(
                    "command_file_invalid_source",
                    "The currently edited command must be updated rather than created.",
                )
            has_effective_change = True
        validate_generated_command_source(
            EXTENSION_BY_FORMAT[change.format], change.content
        )
    if not has_effective_change:
        raise CommandValidationError(
            "command_file_invalid_source",
            "The proposal does not change any command source; return an answer instead.",
        )
