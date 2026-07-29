"""Native adapter construction."""

from guildbotics.intelligences.agent_runtime.claude import ClaudeStreamJsonAdapter
from guildbotics.intelligences.agent_runtime.codex import CodexAppServerAdapter
from guildbotics.intelligences.agent_runtime.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.models import AgentAdapter
from guildbotics.intelligences.agent_runtime.policy import load_native_agent_policy

NATIVE_ADAPTERS = {
    "codex": "codex-app-server",
    "codex-app-server": "codex-app-server",
    "claude": "claude-stream-json",
    "claude-stream-json": "claude-stream-json",
    "grok": "grok-acp",
    "grok-acp": "grok-acp",
}


def create_native_adapter(name: str, person_id: str) -> AgentAdapter:
    adapter = NATIVE_ADAPTERS.get(name, name)
    if adapter == "codex-app-server":
        policy = load_native_agent_policy(person_id)
        return CodexAppServerAdapter(policy=policy.for_adapter("codex"))
    if adapter == "claude-stream-json":
        return ClaudeStreamJsonAdapter()
    if adapter == "grok-acp":
        policy = load_native_agent_policy(person_id)
        return GrokAcpAdapter(policy=policy.for_adapter("grok"))
    raise ValueError(f"Unknown native agent adapter: {name}")
