from typing import cast

from guildbotics.commands.command_base import CommandBase
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.import_utils import ClassResolver


class YamlCommand(CommandBase):
    extensions = [".yaml", ".yml"]
    inline_key = ""

    @classmethod
    def populate_spec(
        cls,
        spec: CommandSpec,
        spec_factory: CommandSpecFactory,
        class_resolver: ClassResolver | None,
    ) -> None:
        if spec.path is None:
            return

        config = cast(dict, load_yaml_file(spec.path))
        spec_factory.populate_spec(spec, config, class_resolver)

    async def run(self) -> CommandOutcome | None:
        return None
