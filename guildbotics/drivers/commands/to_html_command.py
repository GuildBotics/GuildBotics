from __future__ import annotations

from pathlib import Path

from guildbotics.drivers.commands.document_conversion_command import (
    DocumentConversionCommand,
)
from guildbotics.drivers.commands.models import CommandOutcome


class ToHtmlCommand(DocumentConversionCommand):
    """Inline command that converts Markdown input into styled HTML output."""

    extension = ""
    inline_key = "to_html"
    _DEFAULT_CSS_PATH = (
        Path(__file__).resolve().parent.parent.parent
        / "assets"
        / "css"
        / "github-markdown.css"
    )

    async def run(self) -> CommandOutcome:
        inline_config = self._extract_inline_config()
        markdown_source = self._get_input_text(inline_config)
        css_text = self._load_css_text(inline_config)
        html_body = self._render_markdown(markdown_source)
        html_document = self._compose_document(html_body, css_text)

        output_path = self._resolve_output_path(inline_config)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(html_document, encoding="utf-8")
            text_output = str(output_path)
        else:
            text_output = html_document

        return CommandOutcome(result=html_document, text_output=text_output)
