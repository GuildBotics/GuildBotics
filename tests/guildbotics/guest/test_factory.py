from __future__ import annotations

import inspect
import sys

import pytest

from guildbotics.guest.antigravity import AntigravityStreamJsonAdapter
from guildbotics.guest.claude import ClaudeStreamJsonAdapter
from guildbotics.guest.codex import CodexAppServerAdapter
from guildbotics.guest.copilot import CopilotAcpAdapter
from guildbotics.guest.factory import (
    NATIVE_ADAPTERS,
    create_native_adapter,
)
from guildbotics.guest.grok import GrokAcpAdapter
from guildbotics.guest.turn import start_turn
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
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


def test_every_registered_provider_uses_only_its_turns_member_broker() -> None:
    """Keep new registry entries inside the same untrusted-provider boundary:
    a provider runs only as its turn's process, and reaches the member
    commands only through the broker the host binds to the turn -- one broker
    per command, whose port was opened when its environment booted."""
    for adapter_name in set(NATIVE_ADAPTERS.values()):
        modules = [
            sys.modules[cls.__module__]
            for cls in type(create_native_adapter(adapter_name)).__mro__
            if cls.__module__.startswith("guildbotics.")
        ]
        assert any(
            start_turn.__name__ in inspect.getsource(module) for module in modules
        )
        for module in modules:
            assert "TurnBroker(" not in inspect.getsource(module)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", sorted(set(NATIVE_ADAPTERS.values())))
async def test_every_provider_ends_its_turn_however_the_turn_fails(
    adapter_name: str, fake_environment, monkeypatch, tmp_path
) -> None:
    """The command's next turn waits for this one to end, so a turn a
    provider left open would hold the command's environment forever."""

    async def fail(self, *args, **kwargs):
        raise RuntimeError("the provider did not start")

    monkeypatch.setattr(fake_environment, "run", fail)
    adapter = create_native_adapter(adapter_name)
    tool = adapter_name.split("-", 1)[0]
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path,
        conversation_key=ConversationKey("aiko", tool, "manual", "turn"),
    )

    with pytest.raises(RuntimeError, match="the provider did not start"):
        await adapter.run_turn(
            "go",
            context,
            ConversationRecord(key=context.conversation_key),
            lambda _event: None,
        )

    assert fake_environment.started
    assert all(environment.closed for environment in fake_environment.started)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", sorted(set(NATIVE_ADAPTERS.values())))
async def test_every_provider_revokes_the_member_grant_when_interrupted(
    adapter_name: str, fake_environment, tmp_path
) -> None:
    """An interrupted turn is ended at once -- the host revokes its member
    grant and the login it was lent -- before its process is stopped."""
    adapter = create_native_adapter(adapter_name)
    tool = adapter_name.split("-", 1)[0]
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path,
        conversation_key=ConversationKey("aiko", tool, "manual", "turn"),
    )
    environment = await fake_environment.start(context, tool)
    adapter._environment = environment

    await adapter.interrupt()

    assert environment.closed
