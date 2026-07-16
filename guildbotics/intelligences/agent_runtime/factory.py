"""Native adapter construction."""

from guildbotics.intelligences.agent_runtime.claude import ClaudeStreamJsonAdapter
from guildbotics.intelligences.agent_runtime.codex import CodexAppServerAdapter
from guildbotics.intelligences.agent_runtime.models import AgentAdapter
from guildbotics.intelligences.agent_runtime.policy import load_native_agent_policy

NATIVE_ADAPTERS = {
    "codex": "codex-app-server",
    "codex-app-server": "codex-app-server",
    "claude": "claude-stream-json",
    "claude-stream-json": "claude-stream-json",
}


def create_native_adapter(name: str, person_id: str) -> AgentAdapter:
    adapter = NATIVE_ADAPTERS.get(name, name)
    if adapter == "codex-app-server":
        return CodexAppServerAdapter(policy=load_native_agent_policy(person_id))
    if adapter == "claude-stream-json":
        return ClaudeStreamJsonAdapter()
    raise ValueError(f"Unknown native agent adapter: {name}")
