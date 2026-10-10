from __future__ import annotations

from logging import Logger
from pathlib import Path

from pydantic import BaseModel, Field

from guildbotics.intelligences.cli_agents import get_cli_agent_mapping
from guildbotics.intelligences.effort import normalize_effort
from guildbotics.runtime import BrainFactory
from guildbotics.runtime.brain import Brain
from guildbotics.utils.fileio import (
    get_person_config_path,
    load_markdown_with_frontmatter,
    load_person_slot_mapping,
)
from guildbotics.utils.import_utils import ClassResolver, load_class

#: The brain classes the bundled mapping and the settings screen name. They
#: are class paths, not classes: the host reads a mapping without loading the
#: AI CLI brain, which runs only inside a command's isolated environment.
AGNO_BRAIN_CLASS = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain"
CLI_BRAIN_CLASS = "guildbotics.guest.cli_agent.CliAgentBrain"
JEV_BRAIN_CLASS = "guildbotics.intelligences.brains.jev.JevBrain"


class BrainConfig(BaseModel):
    """
    Configuration for a brain.
    """

    class_path: str = Field(..., description="The class path of the brain.")
    args: dict = Field(
        default_factory=dict, description="The arguments for the intelligence."
    )


def get_brain_mapping(person_id: str) -> dict[str, BrainConfig]:
    """Return the person's brain slots, read from configuration each call.

    The mapping is loaded and not stored: remembering the first load served
    stale settings after a hand edit, a sync, a workspace switch, or any write
    that did not go through the settings screen (#587). Re-reading it is cheap
    enough to do on every brain.

    Args:
        person_id (str): The person whose ``brain_mapping.yml`` to read.

    Returns:
        dict[str, BrainConfig]: Slot name to the brain class path and its
            arguments.
    """
    mapping = load_person_slot_mapping(person_id, "intelligences/brain_mapping.yml")
    brain_mapping = {}
    for name, config in mapping.items():
        brain_mapping[name] = BrainConfig(
            class_path=config["class"],
            args=config.get("args", {}),
        )
    return brain_mapping


def cli_agent_of(person_id: str, brain: str) -> str | None:
    """The AI CLI tool the person's brain slot ``brain`` runs.

    Args:
        person_id (str): The person whose mappings to read.
        brain (str): The brain slot a command names.

    Returns:
        str | None: The tool's catalog name; "" when the slot names an AI CLI
        tool slot that does not resolve (its turn says why); None when the
        slot's brain is no AI CLI tool's.
    """
    config = get_brain_mapping(person_id).get(brain)
    if config is None or config.class_path != CLI_BRAIN_CLASS:
        return None
    tool = get_cli_agent_mapping(person_id).get(
        str(config.args.get("cli_agent", "default"))
    )
    return tool.adapter if tool else ""


def command_config(person_id: str, name: str, language_code: str) -> dict:
    """The frontmatter and body of the person's command ``name`` (a name, or
    the path of a ``.md`` file)."""
    if name.endswith(".md"):
        path = Path(name)
    else:
        path = get_person_config_path(person_id, f"commands/{name}.md", language_code)
    return load_markdown_with_frontmatter(path)


class ConfiguredBrainFactory(BrainFactory):
    """Creates the brain a member's ``brain_mapping.yml`` slot names, on the
    host and in a command's isolated environment alike."""

    def create_brain(
        self,
        person_id: str,
        name: str,
        language_code: str,
        logger: Logger,
        config: dict | None = None,
        class_resolver: ClassResolver | None = None,
    ) -> Brain:
        if not config:
            config = command_config(person_id, name, language_code)

        class_resolver = ClassResolver(config.get("schema", ""), class_resolver)
        response_class = None
        response_class_name = config.get("response_class", None)
        if response_class_name:
            response_class = class_resolver.get_model_class(response_class_name)

        description = config.get("body", "")
        template_engine = config.get("template_engine", "default")

        effort = normalize_effort(config.get("effort", ""))

        brain_mapping = get_brain_mapping(person_id)
        brain_config = brain_mapping[config.get("brain", "default")]
        brain = load_class(brain_config.class_path)(
            person_id,
            name,
            logger,
            description,
            template_engine,
            response_class,
            effort=effort,
            **brain_config.args,
        )
        return brain
