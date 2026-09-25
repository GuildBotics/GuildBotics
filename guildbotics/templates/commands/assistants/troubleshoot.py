from __future__ import annotations

from typing import Any

COMMAND_METADATA = {
    "description": "Answer a troubleshooting question from the Desktop diagnostics screen.",
    "read_only": True,
    "inspects": ["diagnostics", "config"],
    "inputs": {"message": "required"},
}


async def main(context: Any, *, conversation_id: str) -> Any:
    """Answer one troubleshooting question about the recorded diagnostics.

    The message is one JSON object: the user's question, what they are looking
    at, and the directories this command's turns inspect. The agent gathers its
    own evidence from those, so nothing else is sent from here. Every turn of
    the same conversation resumes the provider's own session.
    """
    return await context.invoke(
        "functions/troubleshoot",
        agent_execution_context={
            "work_kind": "troubleshooting",
            "work_identity": conversation_id,
            "resume_policy": "auto",
        },
    )
