"""The workspace settings a command's access contract is read from, as a test
states them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_runtime import environment


def settle_contract(monkeypatch: pytest.MonkeyPatch, contract: AccessContract) -> None:
    """Make every command started from now on read ``contract``'s network and
    grants; whether it is read-only stays what the command declares."""
    monkeypatch.setattr(
        environment,
        "load_toolchain",
        lambda: SimpleNamespace(network=contract.network),
    )
    monkeypatch.setattr(environment, "resolve_access", lambda *_: contract.access)
