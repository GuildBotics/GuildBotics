"""AI-assisted troubleshooting over recorded diagnostics.

The Desktop diagnostics screen supplies the user's question and whatever they
are currently looking at. The agent gathers its own evidence through the
read-only ``guildbotics diagnostics`` commands, so nothing but the question and
the focus is sent from this side.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.intelligences.assistants import open_assistant_session
from guildbotics.runtime import Context


class TroubleshootingResult(BaseModel):
    """One assistant answer and the executions it used as evidence."""

    model_config = ConfigDict(extra="forbid")

    message: str
    trace_ids: list[str] = Field(default_factory=list, max_length=10)


async def troubleshoot_turn(
    context: Context,
    *,
    conversation_id: str,
    trace_id: str,
    question: str,
    focus: Mapping[str, Any],
    workspace_data_root: Path,
) -> TroubleshootingResult:
    """Run one resumable AI troubleshooting turn.

    Args:
        context: Context resolved to the member acting as the assistant.
        conversation_id: Stable identity shared by every turn in this conversation.
        trace_id: Unique correlation identity for this turn.
        question: Latest user question.
        focus: What the user is looking at in the diagnostics screen.
        workspace_data_root: Runtime-owned writable data directory.

    Returns:
        The assistant answer and the traces it read as evidence.

    Raises:
        AssistantResponseError: If the configured agent does not return the
            required structured response.
    """
    session = open_assistant_session(
        context,
        prompt="functions/troubleshoot",
        work_kind="troubleshooting",
        conversation_id=conversation_id,
        trace_id=trace_id,
        result_type=TroubleshootingResult,
        workspace_data_root=workspace_data_root,
        cwd_name="troubleshooting",
        read_only=True,
    )
    return await session.send({"question": question, "focus": dict(focus)})
