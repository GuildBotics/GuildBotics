from __future__ import annotations

from builtins import type as builtin_type
from logging import Logger
from pathlib import Path

from pydantic import BaseModel, Field

from guildbotics.intelligences.effort import normalize_effort
from guildbotics.runtime import BrainFactory
from guildbotics.runtime.brain import Brain
from guildbotics.utils.fileio import (
    get_person_config_path,
    load_markdown_with_frontmatter,
    load_person_slot_mapping,
)
from guildbotics.utils.import_utils import ClassResolver, load_class


class BrainConfig(BaseModel):
    """
    Configuration for a brain.
    """

    type: builtin_type[Brain] = Field(..., description="The type of the intelligence.")
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
        dict[str, BrainConfig]: Slot name to the brain class and its arguments.
    """
    mapping = load_person_slot_mapping(person_id, "intelligences/brain_mapping.yml")
    brain_mapping = {}
    for name, config in mapping.items():
        brain_mapping[name] = BrainConfig(
            type=load_class(config["class"]),
            args=config.get("args", {}),
        )
    return brain_mapping


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
            if name.endswith(".md"):
                path = Path(name)
            else:
                path = get_person_config_path(
                    person_id, f"commands/{name}.md", language_code
                )

            config = load_markdown_with_frontmatter(path)

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
        brain = brain_config.type(
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
