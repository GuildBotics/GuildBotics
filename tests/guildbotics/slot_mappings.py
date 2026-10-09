"""A member's slot mappings as a test's configuration holds them.

The brain, model and AI CLI tool mappings are read from the member's
configuration on every use (``get_brain_mapping`` / ``get_model_mapping`` /
``get_cli_agent_mapping``). A test that needs some slots stubs that reading
instead of writing the files.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from guildbotics.intelligences.brains import agno_agent, cli_agent, factory


def use_brain_slots(
    monkeypatch: pytest.MonkeyPatch, person_id: str, slots: dict[str, Any]
) -> None:
    """The member ``person_id``'s brain slots are ``slots``."""
    _use(monkeypatch, factory, "get_brain_mapping", person_id, slots)


def use_model_slots(
    monkeypatch: pytest.MonkeyPatch, person_id: str, slots: dict[str, Any]
) -> None:
    """The member ``person_id``'s model slots are ``slots``."""
    _use(monkeypatch, agno_agent, "get_model_mapping", person_id, slots)


def use_cli_agent_slots(
    monkeypatch: pytest.MonkeyPatch, person_id: str, slots: dict[str, Any]
) -> None:
    """The member ``person_id``'s AI CLI tool slots are ``slots``."""
    _use(monkeypatch, cli_agent, "get_cli_agent_mapping", person_id, slots)


def _use(
    monkeypatch: pytest.MonkeyPatch,
    home: ModuleType,
    name: str,
    person_id: str,
    slots: dict[str, Any],
) -> None:
    """Stub the reading ``home.name`` for ``person_id``: in ``home`` and in
    every module that has imported it by then. A module first imported after
    this keeps the reading itself, so import it before stubbing."""
    read = getattr(home, name)

    def stubbed(person: str) -> dict[str, Any]:
        return slots if person == person_id else read(person)

    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("guildbotics."):
            continue
        for attribute, value in list(vars(module).items()):
            if value is read:
                monkeypatch.setattr(module, attribute, stubbed)
