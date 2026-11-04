"""A context built solely from the host's resolved, non-secret command file."""

from logging import Logger
from pathlib import Path

from guildbotics.entities.team import Person, Project, Team
from guildbotics.intelligences.agent_runtime.wire import ResolvedGuest
from guildbotics.intelligences.brains.agno_agent import (
    AgnoAgentDefaultBrain,
    ModelConfig,
)
from guildbotics.intelligences.brains.factory import command_config
from guildbotics.intelligences.brains.jev import JevBrain
from guildbotics.intelligences.cli_agents import ExecutableInfo
from guildbotics.intelligences.effort import normalize_effort
from guildbotics.runtime.brain import Brain
from guildbotics.runtime.brain_factory import BrainFactory
from guildbotics.utils.import_utils import ClassResolver


class ResolvedBrainFactory(BrainFactory):
    """Construct a resolved slot without reading mappings or model YAML."""

    def __init__(self, data: ResolvedGuest):
        self.data = data

    def create_brain(
        self,
        person_id: str,
        name: str,
        language_code: str,
        logger: Logger,
        config: dict | None = None,
        class_resolver: ClassResolver | None = None,
    ) -> Brain:
        if person_id != self.data.member["person_id"]:
            raise ValueError("The guest may only construct its active member's brain.")
        if config is None:
            path = self.data.commands.get(name)
            if path is None:
                raise ValueError(f"Command '{name}' was not resolved by the host.")
            config = command_config(person_id, str(Path(path)), language_code)
        resolver = ClassResolver(config.get("schema", ""), class_resolver)
        response = config.get("response_class")
        response_class = resolver.get_model_class(response) if response else None
        args = (
            person_id,
            name,
            logger,
            config.get("body", ""),
            config.get("template_engine", "default"),
            response_class,
        )
        effort = normalize_effort(config.get("effort", ""))
        slot = self.data.brains[config.get("brain", "default")]
        if slot.kind == "model":
            return AgnoAgentDefaultBrain(
                *args,
                effort=effort,
                model=slot.slot,
                resolved_model=ModelConfig.model_validate(slot.definition),
            )
        if slot.kind == "ai_cli":
            from guildbotics.guest.cli_agent import CliAgentBrain

            return CliAgentBrain(
                *args,
                effort=effort,
                cli_agent=slot.slot,
                resolved_cli=ExecutableInfo(**slot.definition),
            )
        return JevBrain(*args, effort=effort, model=slot.slot)


def team(data: ResolvedGuest) -> Team:
    """Only the active member crosses the host/guest boundary."""
    project = Project.model_validate({**data.project, "language": data.language})
    return Team(project=project, members=[Person.model_validate(data.member)])
