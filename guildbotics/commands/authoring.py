"""AI-assisted custom-command answers and reviewed change proposals."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guildbotics.commands.formats import EXTENSION_BY_FORMAT, CommandFormat
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_generated_command_source,
)
from guildbotics.intelligences.assistants import open_assistant_session
from guildbotics.runtime import Context

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


async def author_command_turn(
    context: Context,
    *,
    mode: CommandAuthoringMode,
    conversation_id: str,
    trace_id: str,
    command: str,
    command_format: CommandAuthoringFormat | None,
    content: str,
    instruction: str,
    available_commands: list[dict[str, Any]],
    workspace_data_root: Path,
) -> CommandAuthoringResult:
    """Run one resumable AI command-authoring turn.

    Args:
        context: Context resolved to the member acting as the authoring agent.
        mode: Whether this is a new-command request or an existing command.
        conversation_id: Stable identity shared by every turn in this conversation.
        trace_id: Unique correlation identity for this turn.
        command: Current logical shared command name, or empty on initial create.
        command_format: Current command format, or ``None`` on initial create.
        content: Complete current editor buffer, including unsaved changes.
        instruction: Latest user instruction.
        available_commands: Effective shared command sources available for
            read-only inspection and composition.
        workspace_data_root: Runtime-owned writable data directory.

    Returns:
        The assistant answer or complete source-change proposal.

    Raises:
        CommandError: If the configured agent does not return the required
            structured response.
    """
    session = open_assistant_session(
        context,
        prompt="functions/author_command",
        work_kind="command_authoring",
        conversation_id=conversation_id,
        trace_id=trace_id,
        result_type=CommandAuthoringResult,
        workspace_data_root=workspace_data_root,
        cwd_name="command-authoring",
        read_only=True,
    )
    output = await session.send(
        {
            "mode": mode,
            "command": command,
            "format": command_format,
            "current_content": content,
            "instruction": instruction,
            "available_commands": available_commands,
            "allowed_operations": {
                "update_current_command": mode == "edit",
                "create_shared_commands": True,
                "delete_commands": False,
                "change_current_command_format": False,
                "modify_platform_code": False,
            },
        }
    )
    if output.action == "answer":
        return output
    try:
        _validate_proposal(output, mode, command, command_format, content)
    except CommandValidationError as exc:
        output = await session.send(
            {
                "instruction": (
                    "Correct the change proposal without expanding the user's request. "
                    "Return an answer instead if no source change was requested."
                ),
                "original_instruction": instruction,
                "validation_error": str(exc),
                "validation_context": exc.context,
                "invalid_result": output.model_dump(),
            }
        )
        if output.action == "answer":
            return output
        _validate_proposal(output, mode, command, command_format, content)
    return output


def _validate_proposal(
    result: CommandAuthoringResult,
    mode: CommandAuthoringMode,
    command: str,
    command_format: CommandAuthoringFormat | None,
    current_content: str,
) -> None:
    """Validate proposal scope and every generated command source."""
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
