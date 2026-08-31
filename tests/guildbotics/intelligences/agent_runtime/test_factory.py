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
from guildbotics.intelligences.agent_runtime.policy import (
    AdapterFilesystemPolicy,
    NativeAgentPolicy,
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
    monkeypatch, name: str, expected: type, adapter_name: str
) -> None:
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.factory.load_native_agent_policy",
        lambda _person_id: NativeAgentPolicy(),
    )

    adapter = create_native_adapter(name, "aiko")

    assert isinstance(adapter, expected)
    assert adapter.name == adapter_name


def test_each_adapter_only_receives_its_own_policy(monkeypatch) -> None:
    policy = NativeAgentPolicy(
        codex=AdapterFilesystemPolicy(filesystem_access="host"),
        grok=AdapterFilesystemPolicy(filesystem_access="workspace"),
        copilot=AdapterFilesystemPolicy(filesystem_access="host"),
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.factory.load_native_agent_policy",
        lambda _person_id: policy,
    )

    codex = create_native_adapter("codex", "aiko")
    grok = create_native_adapter("grok", "aiko")
    copilot = create_native_adapter("copilot", "aiko")

    assert codex._policy.filesystem_access == "host"
    assert grok._policy.filesystem_access == "workspace"
    assert copilot._policy.filesystem_access == "host"
    # Antigravity takes no filesystem policy: `agy --sandbox` only confines
    # terminal commands, so there is no file scope to hand it.
    assert not hasattr(create_native_adapter("antigravity", "aiko"), "_policy")


def test_unknown_adapter_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown native agent adapter: gemini"):
        create_native_adapter("gemini", "aiko")


def test_every_registered_provider_uses_only_the_member_broker(monkeypatch) -> None:
    """Keep new registry entries inside the same untrusted-provider boundary."""
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.factory.load_native_agent_policy",
        lambda _person_id: NativeAgentPolicy(),
    )

    for adapter_name in set(NATIVE_ADAPTERS.values()):
        adapter = create_native_adapter(adapter_name, "aiko")
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
