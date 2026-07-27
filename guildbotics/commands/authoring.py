"""AI-assisted custom-command authoring.

The App API supplies the latest user instruction and the complete unsaved
editor buffer on every turn. Only the provider conversation is persisted; the
draft remains frontend-owned until the ordinary command-file save endpoint is
used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.commands.errors import CommandError
from guildbotics.commands.formats import EXTENSION_BY_FORMAT, CommandFormat
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_generated_command_source,
)
from guildbotics.intelligences.assistants import open_assistant_session
from guildbotics.runtime import Context

CommandAuthoringMode = Literal["create", "edit"]
CommandAuthoringFormat = CommandFormat


class CommandAuthoringResult(BaseModel):
    """One assistant reply and the resulting complete command draft."""

    model_config = ConfigDict(extra="forbid")

    message: str
    command: str = Field(min_length=1)
    format: CommandAuthoringFormat
    content: str


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
    workspace_data_root: Path,
) -> CommandAuthoringResult:
    """Run one resumable AI command-authoring turn.

    Args:
        context: Context resolved to the member acting as the authoring agent.
        mode: Whether this is an unsaved new command or an existing command.
        conversation_id: Stable identity shared by every turn in this conversation.
        trace_id: Unique correlation identity for this turn.
        command: Current logical shared command name, or empty on initial create.
        command_format: Current command format, or ``None`` on initial create.
        content: Complete current editor buffer, including unsaved changes.
        instruction: Latest user instruction.
        workspace_data_root: Runtime-owned writable data directory.

    Returns:
        The assistant message and complete replacement draft identity and source.

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
    )
    output = await session.send(
        {
            "mode": mode,
            "command": command,
            "format": command_format,
            "current_content": content,
            "instruction": instruction,
        }
    )
    try:
        validate_generated_command_source(
            EXTENSION_BY_FORMAT[output.format], output.content
        )
    except CommandValidationError as exc:
        output = await session.send(
            {
                "instruction": "Correct the proposed draft and return the complete result.",
                "validation_error": str(exc),
                "validation_context": exc.context,
                "invalid_result": output.model_dump(),
            }
        )
        try:
            validate_generated_command_source(
                EXTENSION_BY_FORMAT[output.format], output.content
            )
        except CommandValidationError as remaining:
            output = output.model_copy(
                update={"message": f"{output.message}\n\n{remaining}"}
            )
    if mode == "edit" and (
        output.command != command or output.format != command_format
    ):
        raise CommandError(
            "The command authoring agent cannot rename or change the format of an existing command."
        )
    return output
