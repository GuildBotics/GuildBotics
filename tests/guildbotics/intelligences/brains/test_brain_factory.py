from __future__ import annotations

import logging

import pytest

from guildbotics.intelligences.brains import factory as brain_factory_module
from guildbotics.intelligences.brains.factory import ConfiguredBrainFactory
from guildbotics.intelligences.cli_agents import ExecutableInfo
from guildbotics.intelligences.effort import EffortError
from guildbotics.runtime.brain import Brain
from tests.guildbotics.slot_mappings import class_path


class RecordingBrain(Brain):
    """A brain that only records what the factory handed it."""

    def __init__(self, *args, model: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model

    async def run(self, message: str, **kwargs):  # pragma: no cover - never called
        return message


@pytest.fixture
def _brain_mapping(monkeypatch):
    mapping = {
        "default": brain_factory_module.BrainConfig(
            class_path=class_path(RecordingBrain), args={"model": "default"}
        )
    }
    monkeypatch.setattr(
        brain_factory_module, "get_brain_mapping", lambda person_id: mapping
    )


def _create(config: dict) -> RecordingBrain:
    brain = ConfiguredBrainFactory().create_brain(
        "p1", "functions/reply", "en", logging.getLogger("test"), config=config
    )
    assert isinstance(brain, RecordingBrain)
    return brain


@pytest.mark.usefixtures("_brain_mapping")
def test_frontmatter_effort_reaches_the_brain() -> None:
    assert _create({"body": "prompt", "effort": "high"}).effort == "high"


@pytest.mark.usefixtures("_brain_mapping")
def test_absent_frontmatter_effort_leaves_the_brain_unset() -> None:
    assert _create({"body": "prompt"}).effort == ""


@pytest.mark.usefixtures("_brain_mapping")
def test_brain_specific_arguments_still_reach_the_brain() -> None:
    """Effort is passed by keyword, so it must not displace `model`."""
    brain = _create({"body": "prompt", "effort": "low"})
    assert (brain.effort, brain.model) == ("low", "default")


@pytest.mark.usefixtures("_brain_mapping")
def test_an_unknown_frontmatter_effort_is_rejected() -> None:
    with pytest.raises(EffortError):
        _create({"body": "prompt", "effort": "extreme"})


@pytest.fixture
def _cli_slots(monkeypatch):
    brains = {
        "agent": brain_factory_module.BrainConfig(
            class_path=brain_factory_module.CLI_BRAIN_CLASS,
            args={"cli_agent": "reviewer"},
        ),
        "unmapped": brain_factory_module.BrainConfig(
            class_path=brain_factory_module.CLI_BRAIN_CLASS,
            args={"cli_agent": "missing"},
        ),
        "default": brain_factory_module.BrainConfig(
            class_path=brain_factory_module.AGNO_BRAIN_CLASS
        ),
    }
    monkeypatch.setattr(brain_factory_module, "get_brain_mapping", lambda _: brains)
    monkeypatch.setattr(
        brain_factory_module,
        "get_cli_agent_mapping",
        lambda _: {"reviewer": ExecutableInfo(adapter="claude")},
    )


@pytest.mark.usefixtures("_cli_slots")
@pytest.mark.parametrize(
    ("brain", "tool"),
    [("agent", "claude"), ("unmapped", ""), ("default", None), ("absent", None)],
)
def test_the_tool_a_brain_slot_runs_is_read_from_the_mappings(brain, tool) -> None:
    assert brain_factory_module.cli_agent_of("p1", brain) == tool
