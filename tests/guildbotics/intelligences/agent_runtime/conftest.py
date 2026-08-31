from __future__ import annotations

import pytest

from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
)


@pytest.fixture(autouse=True)
def _adapter_member_broker_without_socket(request, monkeypatch) -> None:
    """Keep adapter tests local; the broker module owns real HTTP coverage."""
    if request.module.__name__.endswith("test_member_broker"):
        return

    async def start(broker: MemberCapabilityBroker) -> None:
        broker._url = "http://127.0.0.1:43123/mcp"

    monkeypatch.setattr(MemberCapabilityBroker, "_start", start)
