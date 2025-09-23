from logging import Logger
from pathlib import Path
from typing import Type, cast

from pydantic import BaseModel, Field

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.runtime import BrainFactory
from guildbotics.utils.fileio import (
    get_person_config_path,
    load_markdown_with_frontmatter,
    load_yaml_file,
)
from guildbotics.utils.import_utils import load_class


class BrainConfig(BaseModel):
    """
    Configuration for a brain.
    """

    type: Type[Brain] = Field(..., description="The type of the intelligence.")
    args: dict = Field(
        default_factory=dict, description="The arguments for the intelligence."
    )


person_brain_mapping: dict[str, dict[str, BrainConfig]] = {}


def get_brain_mapping(person_id: str) -> dict[str, BrainConfig]:
    if person_id in person_brain_mapping:
        return person_brain_mapping[person_id]

    config_file = get_person_config_path(person_id, "intelligences/brain_mapping.yml")
    mapping = cast(dict, load_yaml_file(config_file))
    brain_mapping = {}
    for name, config in mapping.items():
        brain_mapping[name] = BrainConfig(
            type=load_class(config["class"]),
            args=config.get("args", {}),
        )
    person_brain_mapping[person_id] = brain_mapping
    return brain_mapping


class SimpleBrainFactory(BrainFactory):

    def create_brain(
        self, person_id: str, name: str, language_code: str, logger: Logger
    ) -> Brain:
        """
        Create an intelligence instance by name.
        Args:
            name (str): Name of the intelligence to create.
        Returns:
            Intelligence: An instance of the requested intelligence.
        """
        if name.endswith(".md"):
            path = Path(name)
        else:
            path = get_person_config_path(
                person_id, f"intelligences/{name}.md", language_code
            )

        config = cast(dict, load_markdown_with_frontmatter(path))

        response_class = None
        response_class_name = config.get("response_class", None)
        if response_class_name:
            response_class = cast(Type[BaseModel], load_class(response_class_name))

        description = config.get("body", "")
        template_engine = config.get("template_engine", "default")

        brain_mapping = get_brain_mapping(person_id)
        brain_config = brain_mapping[config.get("brain", "default")]
        brain = brain_config.type(
            person_id,
            name,
            logger,
            description,
            template_engine,
            response_class,
            **brain_config.args,
        )
        return brain
