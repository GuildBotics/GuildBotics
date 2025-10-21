from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.models import CommandOutcome


class YamlCommand(CommandBase):
    extensions = [".yaml", ".yml"]
    inline_key = ""

    async def run(self) -> CommandOutcome | None:
        return None
