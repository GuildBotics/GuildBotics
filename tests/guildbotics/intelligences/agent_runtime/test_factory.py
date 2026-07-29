from __future__ import annotations

import pytest

from guildbotics.intelligences.agent_runtime.claude import ClaudeStreamJsonAdapter
from guildbotics.intelligences.agent_runtime.codex import CodexAppServerAdapter
from guildbotics.intelligences.agent_runtime.factory import create_native_adapter
from guildbotics.intelligences.agent_runtime.grok import GrokAcpAdapter
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
    )
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.factory.load_native_agent_policy",
        lambda _person_id: policy,
    )

    codex = create_native_adapter("codex", "aiko")
    grok = create_native_adapter("grok", "aiko")

    assert codex._policy.filesystem_access == "host"
    assert grok._policy.filesystem_access == "workspace"


def test_unknown_adapter_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown native agent adapter: gemini"):
        create_native_adapter("gemini", "aiko")
