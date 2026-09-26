from __future__ import annotations

import json
from typing import Any

from guildbotics.commands.authoring import CommandAuthoringResult, validate_proposal
from guildbotics.commands.errors import CommandError
from guildbotics.commands.validation import CommandValidationError

COMMAND_METADATA = {
    "description": "Answer a request from the Desktop command editor or propose command changes.",
    "read_only": True,
    "inputs": {"message": "required"},
}


async def main(context: Any, *, conversation_id: str) -> CommandAuthoringResult:
    """Answer one command-authoring request, or propose reviewed changes.

    The message is one JSON object: ``mode``, the current ``command`` and
    ``format``, the complete ``current_content`` of the editor, the user's
    ``instruction`` and the ``available_commands``. A proposal that leaves the
    permitted scope is sent back once, in the same conversation, to be
    corrected.
    """
    request = json.loads(context.pipe)

    async def send(payload: dict[str, Any]) -> CommandAuthoringResult:
        output = await context.invoke(
            "functions/author_command",
            message=json.dumps(payload, ensure_ascii=False),
            agent_execution_context={
                "work_kind": "command_authoring",
                "work_identity": conversation_id,
                "resume_policy": "auto",
            },
        )
        if not isinstance(output, CommandAuthoringResult):
            raise CommandError(
                "The command-authoring agent did not return a structured response."
            )
        return output

    def validate(result: CommandAuthoringResult) -> None:
        validate_proposal(
            result,
            request["mode"],
            request["command"],
            request["format"],
            request["current_content"],
        )

    output = await send(
        {
            **request,
            "allowed_operations": {
                "update_current_command": request["mode"] == "edit",
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
        validate(output)
    except CommandValidationError as exc:
        output = await send(
            {
                "instruction": (
                    "Correct the change proposal without expanding the user's request. "
                    "Return an answer instead if no source change was requested."
                ),
                "original_instruction": request["instruction"],
                "validation_error": str(exc),
                "validation_context": exc.context,
                "invalid_result": output.model_dump(),
            }
        )
        if output.action == "answer":
            return output
        validate(output)
    return output
