from __future__ import annotations

import logging

import pytest

from guildbotics.editions.simple import simple_brain_factory
from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.effort import EffortError


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
        "default": simple_brain_factory.BrainConfig(
            type=RecordingBrain, args={"model": "default"}
        )
    }
    monkeypatch.setattr(
        simple_brain_factory, "get_brain_mapping", lambda person_id: mapping
    )


def _create(config: dict) -> RecordingBrain:
    brain = SimpleBrainFactory().create_brain(
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
