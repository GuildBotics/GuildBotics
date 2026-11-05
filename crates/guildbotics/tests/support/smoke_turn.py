"""Run the packaged adapter through the Rust host's turn window."""

import json
import os
from pathlib import Path

from guildbotics.guest.antigravity import AntigravityStreamJsonAdapter
from guildbotics.guest.copilot import CopilotAcpAdapter
from guildbotics.guest.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)

from guildbotics.intelligences.agent_runtime.wire import CommandFacts

_ADAPTERS = {
    "antigravity": AntigravityStreamJsonAdapter,
    "copilot": CopilotAcpAdapter,
    "grok": GrokAcpAdapter,
}


async def main(context):
    request = json.loads(context.pipe)
    tool = request["tool"]
    conversation = ConversationRecord(
        key=ConversationKey(context.person.person_id, tool, "manual", "real-device"),
        provider_session_id=request["session"],
    )
    turn = AgentExecutionContext(
        person_id=context.person.person_id,
        run_id=CommandFacts.read(os.environ).run_id,
        cwd=Path.cwd(),
        conversation_key=conversation.key,
        resume_policy=ResumePolicy.AUTO,
        provider_options=request["options"],
    )
    results = []
    for prompt in request["prompts"]:
        adapter = _ADAPTERS[tool]()
        events = []
        try:
            reply = await adapter.run_turn(prompt, turn, conversation, events.append)
        finally:
            await adapter.close()
        conversation.provider_session_id = reply.provider_session_id
        if tool == "grok":
            sessions = Path.home() / ".grok/sessions"
            signals, = (
                path
                for path in sessions.rglob("signals.json")
                if path.parent.name == reply.provider_session_id
            )
            assert json.loads(signals.read_bytes())["primaryModelId"] == reply.model
        results.append(
            {
                "output": reply.output.strip(),
                "session": reply.provider_session_id,
                "usage": reply.usage,
                "model": reply.model,
                "settings": next(
                    (event.details for event in events if event.name == "settings"), {}
                ),
                "initialized": next(
                    (event.details for event in events if event.name == "initialized"), {}
                ),
                "rehydrated": any(event.name == "history_rehydrated" for event in events),
                "replayed_ok": any(
                    event.name != "thinking" and event.message.strip() == "OK"
                    for event in events
                ),
                "unhandled_turn_completed": any(
                    "turn_completed" in key
                    for event in events
                    if event.name == "protocol_extensions"
                    for key in event.details.get("unhandled", [])
                ),
            }
        )
    return json.dumps(results)
