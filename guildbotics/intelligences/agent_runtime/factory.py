"""Native adapter construction."""

from guildbotics.intelligences.agent_runtime.antigravity import (
    AntigravityStreamJsonAdapter,
)
from guildbotics.intelligences.agent_runtime.claude import ClaudeStreamJsonAdapter
from guildbotics.intelligences.agent_runtime.codex import CodexAppServerAdapter
from guildbotics.intelligences.agent_runtime.copilot import CopilotAcpAdapter
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
    "copilot": "copilot-acp",
    "copilot-acp": "copilot-acp",
    "antigravity": "antigravity-stream-json",
    "antigravity-stream-json": "antigravity-stream-json",
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
    if adapter == "copilot-acp":
        policy = load_native_agent_policy(person_id)
        return CopilotAcpAdapter(policy=policy.for_adapter("copilot"))
    if adapter == "antigravity-stream-json":
        # No filesystem policy: `agy --sandbox` only confines terminal
        # commands, so exposing it as `filesystem_access` would promise a file
        # scope it does not keep.
        return AntigravityStreamJsonAdapter()
    raise ValueError(f"Unknown native agent adapter: {name}")
