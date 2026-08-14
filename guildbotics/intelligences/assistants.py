"""Shared plumbing for resumable, structured Desktop AI assistants.

An assistant turn sends one JSON payload to a member's configured agent and
requires a structured result back. Every turn of the same conversation reuses
one ``work_identity`` so the provider resumes its own session; nothing about
the conversation is persisted on this side.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.commands.errors import CommandError
from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.functions import to_dict
from guildbotics.runtime import Context
from guildbotics.utils.fileio import get_workspace_work_path


class AssistantResponseError(CommandError):
    """Raised when an assistant does not return its required result type."""

    def __init__(self, prompt: str):
        super().__init__(
            f"The '{prompt}' assistant did not return a valid structured response."
        )
        self.prompt = prompt


@dataclass(frozen=True, slots=True)
class AssistantSession[TResult: BaseModel]:
    """One resumable assistant conversation bound to a single turn's trace."""

    prompt: str
    result_type: type[TResult]
    _brain: Brain
    _kwargs: dict[str, Any]

    async def send(self, payload: Mapping[str, Any]) -> TResult:
        """Send one JSON payload and require the structured result type.

        Args:
            payload: Fields serialized as the agent's message for this turn.

        Returns:
            The structured assistant result.

        Raises:
            AssistantResponseError: If the agent returns anything else.
        """
        output = await self._brain.run(
            message=json.dumps(dict(payload), ensure_ascii=False), **self._kwargs
        )
        if not isinstance(output, self.result_type):
            raise AssistantResponseError(self.prompt)
        return output


def open_assistant_session[TResult: BaseModel](
    context: Context,
    *,
    prompt: str,
    work_kind: str,
    conversation_id: str,
    trace_id: str,
    result_type: type[TResult],
    workspace_data_root: Path,
    cwd_name: str,
    read_only: bool = False,
) -> AssistantSession[TResult]:
    """Open one assistant conversation turn against a member's agent.

    Args:
        context: Context resolved to the member acting as the assistant.
        prompt: Prompt template name, such as ``functions/author_command``.
        work_kind: Agent work kind used to scope the provider conversation.
        conversation_id: Stable identity shared by every turn of the conversation.
        trace_id: Unique correlation identity for this turn.
        result_type: Structured response type the agent must return.
        workspace_data_root: Selected GuildBotics workspace root.
        cwd_name: Directory under ``.guildbotics/local/work`` the agent runs in.
        read_only: Whether the agent may only inspect recorded state. Adapters
            enforce this at the provider level, and such a turn takes no member
            execution lease.

    Returns:
        A session that can send one or more payloads for this turn.
    """
    brain = context.get_brain(prompt, None, None)
    execution_context = {
        "run_id": trace_id,
        "work_kind": work_kind,
        "work_identity": conversation_id,
        "resume_policy": "auto",
        "workspace_data_root": str(workspace_data_root),
        "read_only": read_only,
    }
    cwd = get_workspace_work_path(cwd_name, workspace_root=workspace_data_root)
    cwd.mkdir(parents=True, exist_ok=True)
    kwargs = to_dict(context, {"agent_execution_context": execution_context}, cwd)
    return AssistantSession(
        prompt=prompt,
        result_type=result_type,
        _brain=brain,
        _kwargs=kwargs,
    )
