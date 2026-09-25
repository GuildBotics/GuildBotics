"""The next brain sees configuration that changed after the previous one.

A long-lived process (the Desktop local API, ``guildbotics start``) used to
remember the first load of the brain, model, and AI CLI mappings, and only the
settings screen dropped that memory. Hand edits, files synced from another
device, workspace switches, and service-side writes never did. The mappings
are small, so they are read again instead of being remembered.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from guildbotics.editions.simple.simple_brain_factory import (
    SimpleBrainFactory,
    person_brain_mapping,
)
from guildbotics.intelligences.brains.agno_agent import (
    AgnoAgentDefaultBrain,
    person_model_mapping,
)
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentBrain,
    person_cli_agent_mapping,
)
from guildbotics.utils.fileio import get_template_path, load_yaml_file

_PERSON = "kenji"
_MODEL_CLASS = "agno.models.openai.OpenAIChat"
_BRAIN_CLASS = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain"


@pytest.fixture(autouse=True)
def _no_mapping_override() -> None:
    person_brain_mapping.clear()
    person_model_mapping.clear()
    person_cli_agent_mapping.clear()


def _use(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))


def _config(tmp_path: Path, name: str) -> Path:
    config = tmp_path / name / ".guildbotics" / "config"
    (config / "intelligences" / "models" / "openai").mkdir(parents=True)
    (config / "intelligences" / "cli_agents" / "codex").mkdir(parents=True)
    return config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _model_file(config: Path, model_id: str, slot: str = "default") -> None:
    _write(
        config / "intelligences" / "models" / "openai" / f"{slot}.yml",
        f"model_class: {_MODEL_CLASS}\nparameters:\n  id: {model_id}\n",
    )


def _layouts(config: Path, *, model_id: str, cli_model: str, brain_slot: str) -> None:
    intel = config / "intelligences"
    _model_file(config, model_id)
    _model_file(config, f"{model_id}-writer", slot="writer")
    _write(
        intel / "model_mapping.yml",
        "default: models/openai/default.yml\nwriter: models/openai/writer.yml\n",
    )
    _write(intel / "cli_agent_mapping.yml", "default: cli_agents/codex/default.yml\n")
    _write(
        intel / "cli_agents" / "codex" / "default.yml",
        f"parameters:\n  model: {cli_model}\n",
    )
    _write(
        intel / "brain_mapping.yml",
        f"default:\n  class: {_BRAIN_CLASS}\n  args:\n    model: {brain_slot}\n",
    )


def _model_id() -> str:
    brain = AgnoAgentDefaultBrain(_PERSON, "reply", logging.getLogger("test"))
    return str(brain.model_config.parameters["id"])


def _cli_model() -> str:
    brain = CliAgentBrain(_PERSON, "reply", logging.getLogger("test"))
    return str(brain.executable_info.parameters["model"])


def _brain_slot() -> str:
    brain = SimpleBrainFactory().create_brain(
        _PERSON,
        "reply",
        "en",
        logging.getLogger("test"),
        config={"body": "prompt"},
    )
    assert isinstance(brain, AgnoAgentDefaultBrain)
    return brain.model_slot


def _assert_nothing_was_remembered() -> None:
    assert person_model_mapping == {}
    assert person_cli_agent_mapping == {}
    assert person_brain_mapping == {}


def test_replacing_files_is_visible_to_the_next_brain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand edit, a sync, or a service write is the same thing: new bytes."""
    config = _config(tmp_path, "ws")
    _layouts(config, model_id="model-old", cli_model="cli-old", brain_slot="default")
    _use(monkeypatch, config)

    assert (_model_id(), _cli_model(), _brain_slot()) == (
        "model-old",
        "cli-old",
        "default",
    )

    _layouts(config, model_id="model-new", cli_model="cli-new", brain_slot="writer")

    assert (_model_id(), _cli_model(), _brain_slot()) == (
        "model-new",
        "cli-new",
        "writer",
    )
    _assert_nothing_was_remembered()


def test_switching_workspace_uses_that_workspaces_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``set_workspace`` publishes a new config dir; the next brain follows it."""
    first = _config(tmp_path, "one")
    second = _config(tmp_path, "two")
    _layouts(first, model_id="from-one", cli_model="cli-one", brain_slot="default")
    _layouts(second, model_id="from-two", cli_model="cli-two", brain_slot="writer")

    _use(monkeypatch, first)
    assert (_model_id(), _cli_model(), _brain_slot()) == (
        "from-one",
        "cli-one",
        "default",
    )

    _use(monkeypatch, second)
    assert (_model_id(), _cli_model(), _brain_slot()) == (
        "from-two",
        "cli-two",
        "writer",
    )
    _assert_nothing_was_remembered()


def test_a_member_file_added_after_the_first_brain_is_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path, "ws")
    _layouts(config, model_id="team-model", cli_model="cli-team", brain_slot="default")
    _use(monkeypatch, config)
    assert _model_id() == "team-model"

    _model_file(
        config / "team" / "members" / _PERSON,
        "member-model",
    )

    assert _model_id() == "member-model"
    _assert_nothing_was_remembered()


def test_removing_the_workspace_model_falls_back_to_the_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path, "ws")
    _layouts(config, model_id="workspace-only", cli_model="cli", brain_slot="default")
    _use(monkeypatch, config)
    assert _model_id() == "workspace-only"

    (config / "intelligences" / "model_mapping.yml").unlink()
    (config / "intelligences" / "models" / "openai" / "default.yml").unlink()
    (config / "intelligences" / "models" / "openai" / "writer.yml").unlink()

    template = load_yaml_file(
        get_template_path() / "intelligences/models/openai/default.yml"
    )
    assert _model_id() == template["parameters"]["id"]
    _assert_nothing_was_remembered()
