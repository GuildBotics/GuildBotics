from __future__ import annotations

from guildbotics.drivers.commands.markdown_command import MarkdownCommand
from guildbotics.drivers.commands.models import CommandOutcome


class PrintCommand(MarkdownCommand):
    """
    A Markdown command that prints output using the 'print' inline key.

    This command is inline-only and sets the template engine to 'jinja2' and disables the 'brain' feature.
    It differs from the base MarkdownCommand by customizing the config for template rendering and disabling
    any AI/brain processing.
    """

    extension = ""
    inline_key = "print"

    async def run(self) -> CommandOutcome | None:
        self.spec.config["template_engine"] = "jinja2"
        self.spec.config["brain"] = "none"
        return await super().run()
