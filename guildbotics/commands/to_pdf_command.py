from __future__ import annotations

import base64
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from guildbotics.commands.document_conversion_command import DocumentConversionCommand
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome

_HOMEBREW_PREFIX_ENV = "HOMEBREW_PREFIX"
_DEFAULT_HOMEBREW_PREFIX = "/opt/homebrew"
_DYLD_LIBRARY_PATH_ENV = "DYLD_LIBRARY_PATH"
_WEASYPRINT_MODULE = "weasyprint"

# Serializes the environment override below. ``os.environ`` is process-global
# while the task scheduler runs one worker thread per active member, so two
# concurrent conversions would otherwise save and restore overlapping values
# and leave the Homebrew path behind.
_library_path_lock = threading.Lock()


@contextmanager
def _homebrew_library_path() -> Iterator[None]:
    """Make Homebrew's native libraries discoverable on macOS.

    WeasyPrint loads libraries such as ``gobject-2.0`` through
    ``ctypes.util.find_library``, which on macOS only searches ``~/lib``,
    ``/usr/local/lib``, ``/lib`` and ``/usr/lib``. Homebrew's Apple Silicon
    prefix is not among them, so the import fails even when the libraries are
    installed. A leftover Intel Homebrew tree makes it worse: its x86_64
    libraries in ``/usr/local/lib`` are found first and then fail to load.

    Prepending Homebrew's library directory resolves both cases while keeping
    the default directories as fallbacks. The variable is restored on exit so
    that it is not inherited by subprocesses, and the whole save/override/
    restore cycle is held under a process-wide lock so that concurrent member
    workers cannot interleave it. Once WeasyPrint is loaded the override is
    skipped entirely, which keeps the window down to the first conversion in
    the process.
    """
    lib_dir = Path(
        os.environ.get(_HOMEBREW_PREFIX_ENV, _DEFAULT_HOMEBREW_PREFIX), "lib"
    )
    if (
        sys.platform != "darwin"
        or _WEASYPRINT_MODULE in sys.modules
        or not lib_dir.is_dir()
    ):
        yield
        return

    with _library_path_lock:
        previous = os.environ.get(_DYLD_LIBRARY_PATH_ENV)
        entries = [previous, str(lib_dir)] if previous else [str(lib_dir)]
        os.environ[_DYLD_LIBRARY_PATH_ENV] = os.pathsep.join(entries)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_DYLD_LIBRARY_PATH_ENV, None)
            else:
                os.environ[_DYLD_LIBRARY_PATH_ENV] = previous


class ToPdfCommand(DocumentConversionCommand):
    """Inline command that converts Markdown or HTML input into a PDF document."""

    extensions: ClassVar[list[str]] = []
    inline_key: ClassVar[str] = "to_pdf"
    _DEFAULT_CSS_PATH = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "css"
        / "github-markdown-light.css"
    )

    async def run(self) -> CommandOutcome:
        try:
            with _homebrew_library_path():
                from weasyprint import HTML  # type: ignore
        except (ImportError, OSError) as exc:
            raise CommandError(
                "PDF conversion requires WeasyPrint native dependencies."
            ) from exc

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
