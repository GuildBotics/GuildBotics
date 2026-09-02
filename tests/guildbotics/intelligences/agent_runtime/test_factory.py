from __future__ import annotations

import inspect
import sys

import pytest

from guildbotics.intelligences.agent_runtime.antigravity import (
    AntigravityStreamJsonAdapter,
)
from guildbotics.intelligences.agent_runtime.claude import ClaudeStreamJsonAdapter
from guildbotics.intelligences.agent_runtime.codex import CodexAppServerAdapter
from guildbotics.intelligences.agent_runtime.copilot import CopilotAcpAdapter
from guildbotics.intelligences.agent_runtime.factory import (
    NATIVE_ADAPTERS,
    create_native_adapter,
)
from guildbotics.intelligences.agent_runtime.grok import GrokAcpAdapter
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
)


@pytest.mark.parametrize(
    ("name", "expected", "adapter_name"),
    [
        ("codex", CodexAppServerAdapter, "codex-app-server"),
        ("codex-app-server", CodexAppServerAdapter, "codex-app-server"),
        ("claude", ClaudeStreamJsonAdapter, "claude-stream-json"),
        ("grok", GrokAcpAdapter, "grok-acp"),
        ("grok-acp", GrokAcpAdapter, "grok-acp"),
        ("copilot", CopilotAcpAdapter, "copilot-acp"),
        ("copilot-acp", CopilotAcpAdapter, "copilot-acp"),
        ("antigravity", AntigravityStreamJsonAdapter, "antigravity-stream-json"),
        (
            "antigravity-stream-json",
            AntigravityStreamJsonAdapter,
            "antigravity-stream-json",
        ),
    ],
)
def test_aliases_resolve_to_their_adapter(
    name: str, expected: type, adapter_name: str
) -> None:
    adapter = create_native_adapter(name)

    assert isinstance(adapter, expected)
    assert adapter.name == adapter_name


def test_unknown_adapter_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown native agent adapter: gemini"):
        create_native_adapter("gemini")


def test_every_registered_provider_uses_only_the_member_broker() -> None:
    """Keep new registry entries inside the same untrusted-provider boundary."""
    for adapter_name in set(NATIVE_ADAPTERS.values()):
        adapter = create_native_adapter(adapter_name)
        assert isinstance(adapter._member_broker, MemberCapabilityBroker)
        modules = {
            sys.modules[adapter_type.__module__]
            for adapter_type in type(adapter).__mro__
            if adapter_type.__module__.startswith(
                "guildbotics.intelligences.agent_runtime"
            )
        }
        for module in modules:
            source = inspect.getsource(module)
            assert "delegation_environment(" not in source
            assert "member_command_environment(" not in source
