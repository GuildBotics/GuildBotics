"""Native adapter construction."""

from guildbotics.guest.antigravity import AntigravityStreamJsonAdapter
from guildbotics.guest.claude import ClaudeStreamJsonAdapter
from guildbotics.guest.codex import CodexAppServerAdapter
from guildbotics.guest.copilot import CopilotAcpAdapter
from guildbotics.guest.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.models import AgentAdapter

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


def create_native_adapter(name: str) -> AgentAdapter:
    adapter = NATIVE_ADAPTERS.get(name, name)
    if adapter == "codex-app-server":
        return CodexAppServerAdapter()
    if adapter == "claude-stream-json":
        return ClaudeStreamJsonAdapter()
    if adapter == "grok-acp":
        return GrokAcpAdapter()
    if adapter == "copilot-acp":
        return CopilotAcpAdapter()
    if adapter == "antigravity-stream-json":
        return AntigravityStreamJsonAdapter()
    raise ValueError(f"Unknown native agent adapter: {name}")
