from __future__ import annotations

from typing import Any

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.errors import CommandError
from guildbotics.drivers.commands.models import CommandOutcome, CommandSpec
from guildbotics.drivers.commands.spec_factory import CommandSpecFactory
from guildbotics.drivers.commands.utils import stringify_output
from guildbotics.intelligences.functions import get_content, preprocess, to_dict
from guildbotics.utils.fileio import load_markdown_with_frontmatter
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.text_utils import replace_placeholders


class MarkdownCommand(CommandBase):
    extensions = [".md"]
    inline_key = "prompt"

    @classmethod
    def populate_spec(
        cls,
        spec: CommandSpec,
        spec_factory: CommandSpecFactory,
        class_resolver: ClassResolver | None,
    ) -> None:
        if spec.path is None:
            return

        config = load_markdown_with_frontmatter(spec.path)
        spec_factory.populate_spec(spec, config, class_resolver)

    async def run(self) -> CommandOutcome | None:
        config, inline = self._load_markdown_metadata()
        if not config.get("body"):
            return None

        params = {**self.context.shared_state, **self.options.params}
        if self._is_brain_disabled(config):
            template_engine = config.get("template_engine", "default")
            params = self._inject_session_state(params)
            result = replace_placeholders(config["body"], params, template_engine)
            return CommandOutcome(result=result, text_output=result)

        message = self.options.message
        if not preprocess(self.context, message):
            message = ""

        try:
            output = await get_content(
                self.context,
                str(self.spec.path),
                message,
                params,
                self.cwd,
                config if inline else None,
                self.spec.class_resolver,
            )
        except Exception as exc:  # pragma: no cover - propagate as driver error
            raise CommandError(
                f"Custom command '{self.spec.name}' execution failed: {exc}"
            ) from exc

        text_output = stringify_output(output)
        return CommandOutcome(result=output, text_output=text_output)

    def _inject_session_state(self, params: dict[str, Any]) -> dict[str, Any]:
        session_data = to_dict(self.context, {})
        session_state = session_data.get("session_state", {})
        return {**params, **session_state}

    def _is_brain_disabled(self, config: dict[str, Any]) -> bool:
        brain = str(config.get("brain", "")).lower()
        return brain in {"none", "-", "null", "disabled"}

    def _load_markdown_metadata(self) -> tuple[dict[str, Any], bool]:
        prompt = self.spec.get_config_value(self.inline_key)
        if prompt is not None:
            config = self.spec.config.copy()
            config["body"] = str(prompt)
            return config, True

        if self.spec.path is None:
            raise CommandError(
                f"Markdown command '{self.spec.name}' is missing a path or {self.inline_key}."
            )
        config = load_markdown_with_frontmatter(self.spec.path)
        return config, False
