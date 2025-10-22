from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from markdown_it import MarkdownIt

from guildbotics.commands.command_base import CommandBase
from guildbotics.commands.errors import CommandError


class DocumentConversionCommand(CommandBase):
    """Base class providing shared helpers for inline document conversion commands."""

    _DEFAULT_CSS_PATH: ClassVar[Path]
    inline_key: ClassVar[str]

    _MARKDOWN_RENDERER = MarkdownIt()

    def _extract_inline_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {}
        value = self.spec.get_config_value(self.inline_key)
        if value is None:
            return config

        if isinstance(value, dict):
            config.update(value)
        elif isinstance(value, str):
            args, params = self.parse_inline_args(value)
            config.update(params)
            if "output" not in config and args:
                config["output"] = args[0]
        else:
            raise CommandError(
                f"Inline '{self.inline_key}' configuration must be a string or mapping."
            )
        return config

    def _get_input_text(self, inline_config: dict[str, Any]) -> str:
        input_value = self._get_preferred_value("input", inline_config)
        if input_value is not None and str(input_value).strip() != "":
            input_path = self._resolve_existing_path(
                str(input_value), "Input file", search_base_dir=True
            )
            try:
                return input_path.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - defensive guard
                raise CommandError(
                    f"Unable to read input file '{input_path}': {exc}"
                ) from exc

        return self.options.message

    def _load_css_text(self, inline_config: dict[str, Any]) -> str:
        css_value = self._get_preferred_value("css", inline_config)
        if css_value is None or str(css_value).strip() == "":
            css_path = self._DEFAULT_CSS_PATH
        else:
            css_path = self._resolve_existing_path(str(css_value), "CSS file")

        try:
            return css_path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive guard
            raise CommandError(f"Unable to read CSS file '{css_path}': {exc}") from exc

    def _render_markdown(self, markdown_source: str) -> str:
        return self._MARKDOWN_RENDERER.render(markdown_source)

    def _compose_document(self, body_html: str, css_text: str) -> str:
        head_parts = ['<meta charset="utf-8">']
        if css_text:
            head_parts.append("<style>")
            head_parts.append(css_text)
            head_parts.append("</style>")

        head_html = "\n".join(head_parts)
        return (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "<head>\n"
            f"{head_html}\n"
            "</head>\n"
            '<body class="markdown-body">\n'
            f"{body_html}\n"
            "</body>\n"
            "</html>\n"
        )

    def _resolve_output_path(self, inline_config: dict[str, Any]) -> Path | None:
        output_value = self._get_preferred_value("output", inline_config)
        if output_value is None:
            return None

        output_path = Path(str(output_value)).expanduser()
        if not output_path.is_absolute():
            output_path = (self.cwd / output_path).resolve()
        return output_path

    def _get_preferred_value(
        self, key: str, inline_config: dict[str, Any]
    ) -> Any | None:
        if key in self.options.params:
            return self.options.params[key]
        if key in inline_config:
            return inline_config[key]
        return self.spec.get_config_value(key)

    def _resolve_existing_path(
        self, raw_path: str, description: str, search_base_dir: bool = False
    ) -> Path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute() and candidate.exists():
            return candidate

        candidate_paths = []
        if search_base_dir:
            candidate_paths.append((self.spec.base_dir / candidate).resolve())
        candidate_paths.extend(
            [
                (self.cwd / candidate).resolve(),
                (self.spec.base_dir / candidate).resolve(),
            ]
        )

        seen: set[Path] = set()
        relative_candidates: list[Path] = []
        for path in candidate_paths:
            if path not in seen:
                seen.add(path)
                relative_candidates.append(path)

        for path in relative_candidates:
            if path.exists():
                return path

        raise CommandError(f"{description} '{raw_path}' could not be found.")
