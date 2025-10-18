from __future__ import annotations

from guildbotics.drivers.commands.markdown_command import MarkdownCommand
from guildbotics.drivers.commands.models import CommandOutcome


class PrintCommand(MarkdownCommand):
    extension = ".md"
    shortcut = "print"
    shortcut_only = True

    async def run(self) -> CommandOutcome | None:
        self.spec.config["template_engine"] = "jinja2"
        self.spec.config["brain"] = "none"
        return await super().run()
