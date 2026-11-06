"""The translate sample renders in the guest as the OS UI language says."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.utils.fileio import load_markdown_with_frontmatter
from guildbotics.utils.text_utils import replace_placeholders

SAMPLES = Path(__file__).parents[3] / "guildbotics/templates/sample_commands"


@pytest.mark.parametrize(
    ("template_language", "language_code", "language_name", "expected"),
    [
        ("ja", "ja", "日本語", "`input`フィールドのテキストが日本語であれば英語に"),
        ("ja", "en", "English", "`input`フィールドのテキストが日本語であれば英語に"),
        ("ja", "fr", "français", "`input`フィールドのテキストがfrançaisであれば英語に"),
        ("en", "ja", "日本語", "text in the `input` field is in 日本語"),
        ("en", "en", "English", "text in the `input` field is in Japanese"),
        ("en", "fr", "français", "text in the `input` field is in français"),
    ],
)
def test_translate_sample_uses_os_ui_language_without_arguments(
    template_language: str,
    language_code: str,
    language_name: str,
    expected: str,
) -> None:
    command = load_markdown_with_frontmatter(
        SAMPLES / template_language / "translate.md"
    )
    os_ui_language = SimpleNamespace(
        language_code=language_code, language_name=language_name
    )

    rendered = replace_placeholders(
        command["body"], {"os_ui_language": os_ui_language}, command["template_engine"]
    )

    assert expected in rendered
