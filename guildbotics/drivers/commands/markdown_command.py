from __future__ import annotations

from typing import Any

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.errors import CustomCommandError
from guildbotics.drivers.commands.models import CommandOutcome, CommandSpec
from guildbotics.drivers.commands.utils import stringify_output
from guildbotics.intelligences.functions import get_content, preprocess, to_dict
from guildbotics.utils.fileio import load_markdown_with_frontmatter
from guildbotics.utils.text_utils import replace_placeholders


class MarkdownCommand(CommandBase):
    extension = ".md"
    shortcut = "prompt"

    async def run(self) -> CommandOutcome | None:
        metadata, inline = load_markdown_metadata(self.spec)
        if not metadata.get("body"):
            return None

        params = {**self.context.shared_state, **self.options.params}
        if self._is_brain_disabled(metadata):
            template_engine = metadata.get("template_engine", "default")
            params = self._inject_session_state(params)
            result = replace_placeholders(metadata["body"], params, template_engine)
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
                metadata if inline else None,
                self.spec.class_resolver,
            )
        except Exception as exc:  # pragma: no cover - propagate as driver error
            raise CustomCommandError(
                f"Custom command '{self.spec.name}' execution failed: {exc}"
            ) from exc

        text_output = stringify_output(output)
        return CommandOutcome(result=output, text_output=text_output)

    def _inject_session_state(self, params: dict[str, Any]) -> dict[str, Any]:
        session_data = to_dict(self.context, {})
        session_state = session_data.get("session_state", {})
        return {**params, **session_state}

    def _is_brain_disabled(self, metadata: dict[str, Any]) -> bool:
        brain = str(metadata.get("brain", "")).lower()
        return brain in {"none", "-", "null", "disabled"}


def load_markdown_metadata(
    spec: CommandSpec,
) -> tuple[dict[str, Any], bool]:
    prompt = spec.get_config_value("prompt")
    if spec.metadata is not None:
        return spec.metadata, bool(prompt)

    if prompt is not None:
        config = spec.config if isinstance(spec.config, dict) else {}
        metadata = config.copy()
        metadata["body"] = str(prompt)
        spec.metadata = metadata
        return metadata, True

    metadata = load_markdown_with_frontmatter(spec.path)
    spec.metadata = metadata
    return metadata, False
