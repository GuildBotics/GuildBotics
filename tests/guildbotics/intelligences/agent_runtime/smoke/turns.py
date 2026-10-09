"""A tool's turns, run as the product runs them.

Every turn runs inside the microVM of the command it belongs to: the command
is started as the host starts one, and inside it GuildBotics' own code starts
the provider through the adapter, the host lending the turn its login. The
smoke tests ask for their turns here and read back what the adapter reported:
its result, or its failure, and the events it emitted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
)
from guildbotics.intelligences.brains import cli_agent
from tests.guildbotics.product_path import run_file, workspace_member
from tests.guildbotics.slot_mappings import use_cli_agent_slots

#: The command the turns run in: it drives each turn through the adapter
#: inside its microVM, working where the command does, one conversation
#: carried from turn to turn, and answers with what each reported.
_TURNS = """
import importlib, json, os
from dataclasses import asdict
from pathlib import Path

from guildbotics.intelligences.agent_runtime.host_client import CommandFacts
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext, AgentRuntimeError, ConversationKey, ConversationRecord,
    ResumePolicy,
)


async def main(context):
    asked = json.loads(context.pipe)
    facts = CommandFacts.read(os.environ)
    module, _, name = asked["adapter"].rpartition(".")
    turn = AgentExecutionContext(
        person_id=facts.person_id,
        run_id=facts.run_id,
        cwd=Path.cwd(),
        conversation_key=ConversationKey(
            facts.person_id, asked["tool"], "manual", "smoke"
        ),
        resume_policy=ResumePolicy.AUTO,
        provider_options=asked["options"],
    )
    conversation = ConversationRecord(
        key=turn.conversation_key, provider_session_id=asked["session"]
    )
    answers = []
    for prompt in asked["prompts"]:
        adapter = getattr(importlib.import_module(module), name)()
        events = []
        try:
            result = await adapter.run_turn(prompt, turn, conversation, events.append)
            conversation.provider_session_id = result.provider_session_id
            answer = {
                "result": {
                    key: value
                    for key, value in asdict(result).items()
                    if key != "events"
                }
            }
        except AgentRuntimeError as exc:
            answer = {
                "error": {
                    "category": exc.category.value,
                    "message": str(exc),
                    "details": exc.details,
                }
            }
        finally:
            await adapter.close()
        answer["events"] = [asdict(event) for event in events]
        answers.append(answer)
    return json.dumps(answers, default=str)
"""


async def run_turns(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    tool: str,
    adapter: type,
    prompts: Sequence[str],
    *,
    session: str = "",
    options: dict[str, Any] | None = None,
    access: CommandAccess | None = None,
    held: Path | None = None,
) -> list[tuple[AgentTerminalResult | AgentRuntimeError, list[AgentEvent]]]:
    """Run ``prompts`` as turns of ``tool`` through ``adapter``, one after
    another, in one command working in ``work``, resuming ``session``.

    The command runs as the workspace's default member, configured for this
    tool alone, and declares ``access`` (nothing by default). Its file is in
    ``held`` (``work`` by default): a read-only command's working directory
    holds nothing of the host, so its file has to be where it is granted.

    Returns:
        For each turn, what the adapter returned, or the failure it raised,
        and the events it emitted.
    """
    use_cli_agent_slots(
        monkeypatch,
        workspace_member(),
        {"default": cli_agent.ExecutableInfo(adapter=tool)},
    )
    command = (held or work) / "smoke_turns.py"
    command.write_text(_TURNS, encoding="utf-8")
    asked = {
        "adapter": f"{adapter.__module__}.{adapter.__qualname__}",
        "tool": tool,
        "prompts": list(prompts),
        "session": session,
        "options": options or {},
    }
    try:
        outcome = await run_file(
            command, json.dumps(asked), cwd=work, access=access or CommandAccess()
        )
    finally:
        command.unlink()
    return [_answer(answer) for answer in json.loads(outcome.text_output)]


def _answer(
    answer: dict[str, Any],
) -> tuple[AgentTerminalResult | AgentRuntimeError, list[AgentEvent]]:
    events = [
        AgentEvent(**{**event, "kind": AgentEventKind(event["kind"])})
        for event in answer["events"]
    ]
    if "error" in answer:
        error = answer["error"]
        return (
            AgentRuntimeError(
                AgentRuntimeErrorCategory(error["category"]),
                error["message"],
                details=error["details"],
            ),
            events,
        )
    return AgentTerminalResult(**answer["result"], events=tuple(events)), events
