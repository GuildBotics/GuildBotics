"""AI-assisted custom-command authoring.

The App API supplies the latest user instruction and the complete unsaved
editor buffer on every turn. Only the provider conversation is persisted; the
draft remains frontend-owned until the ordinary command-file save endpoint is
used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.commands.errors import CommandError
from guildbotics.commands.formats import EXTENSION_BY_FORMAT, CommandFormat
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_generated_command_source,
)
from guildbotics.intelligences.functions import to_dict
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
    authoring_id: str,
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
        authoring_id: Stable identity shared by every turn in this conversation.
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
    brain = context.get_brain("functions/author_command", None, None)
    prompt = json.dumps(
        {
            "mode": mode,
            "command": command,
            "format": command_format,
            "current_content": content,
            "instruction": instruction,
        },
        ensure_ascii=False,
    )
    execution_context = {
        "run_id": trace_id,
        "work_kind": "command_authoring",
        "work_identity": authoring_id,
        "resume_policy": "auto",
        "workspace_data_root": str(workspace_data_root),
    }
    authoring_cwd = workspace_data_root / "command-authoring"
    authoring_cwd.mkdir(parents=True, exist_ok=True)
    kwargs = to_dict(
        context,
        {"agent_execution_context": execution_context},
        authoring_cwd,
    )
    output = await brain.run(message=prompt, **kwargs)
    if not isinstance(output, CommandAuthoringResult):
        raise CommandError(
            "The command authoring agent did not return a valid structured response."
        )
    try:
        validate_generated_command_source(
            EXTENSION_BY_FORMAT[output.format], output.content
        )
    except CommandValidationError as exc:
        correction = json.dumps(
            {
                "instruction": "Correct the proposed draft and return the complete result.",
                "validation_error": str(exc),
                "validation_context": exc.context,
                "invalid_result": output.model_dump(),
            },
            ensure_ascii=False,
        )
        output = await brain.run(message=correction, **kwargs)
        if not isinstance(output, CommandAuthoringResult):
            raise CommandError(
                "The command authoring agent did not return a valid structured response."
            ) from None
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
