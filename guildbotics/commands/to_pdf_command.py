from __future__ import annotations

import base64
from pathlib import Path

from weasyprint import HTML  # type: ignore

from guildbotics.commands.document_conversion_command import DocumentConversionCommand
from guildbotics.commands.models import CommandOutcome


class ToPdfCommand(DocumentConversionCommand):
    """Inline command that converts Markdown or HTML input into a PDF document."""

    extensions = []
    inline_key = "to_pdf"
    _DEFAULT_CSS_PATH = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "css"
        / "github-markdown-light.css"
    )

    async def run(self) -> CommandOutcome:
        inline_config = self._extract_inline_config()
        source_text = self._get_input_text(inline_config)
        css_text = self._load_css_text(inline_config)

        if self._contains_html_root(source_text):
            html_document = self._apply_css_to_html_document(source_text, css_text)
        else:
            body_html = self._render_markdown(source_text)
            html_document = self._compose_document(body_html, css_text)
        pdf_bytes = HTML(string=html_document, base_url=str(self.cwd)).write_pdf()

        output_path = self._resolve_output_path(inline_config)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            text_output = str(output_path)
        else:
            text_output = base64.b64encode(pdf_bytes).decode("ascii")

        return CommandOutcome(result=pdf_bytes, text_output=text_output)

    def _contains_html_root(self, source: str) -> bool:
        lowered = source.lower()
        return "<html" in lowered and "</html>" in lowered

    def _apply_css_to_html_document(self, html_document: str, css_text: str) -> str:
        if not css_text:
            return html_document

        lowered = html_document.lower()
        close_index = lowered.find("</head>")
        style_block = "<style>\n" + css_text + "\n</style>\n"
        if close_index != -1:
            return (
                html_document[:close_index] + style_block + html_document[close_index:]
            )
        return style_block + html_document
