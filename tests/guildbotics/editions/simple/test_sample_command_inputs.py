"""Input-contract coverage for localized sample commands."""

from pathlib import Path

import pytest

from guildbotics.utils.fileio import load_markdown_with_frontmatter, load_yaml_file


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("context-info", "hidden"),
        ("summarize", "hidden"),
        ("translate", "required"),
    ],
)
@pytest.mark.parametrize("language", ["en", "ja"])
def test_markdown_sample_command_inputs(
    command: str, message: str, language: str
) -> None:
    path = (
        Path("guildbotics/editions/simple/templates/sample_commands")
        / language
        / f"{command}.md"
    )

    metadata = load_markdown_with_frontmatter(path)

    assert metadata["inputs"] == {"message": message}


@pytest.mark.parametrize("language", ["en", "ja"])
def test_yaml_sample_command_inputs(language: str) -> None:
    path = (
        Path("guildbotics/editions/simple/templates/sample_commands")
        / language
        / "get-time-of-day.yml"
    )

    metadata = load_yaml_file(path)

    assert metadata["inputs"] == {"message": "hidden"}


@pytest.mark.parametrize("language", ["en", "ja"])
def test_get_time_of_day_uses_selected_member_speaking_style(language: str) -> None:
    path = (
        Path("guildbotics/editions/simple/templates/sample_commands")
        / language
        / "get-time-of-day.yml"
    )

    metadata = load_yaml_file(path)
    greeting = metadata["commands"][-1]
    prompt = greeting["prompt"]

    assert greeting["template_engine"] == "jinja2"
    assert "{{ context.person.name }}" in prompt
    assert "{{ context.person.speaking_style }}" in prompt
    assert "{{ current_time }}" in prompt
    assert "{{ time_of_day.label }}" in prompt


@pytest.mark.parametrize(
    ("language", "default_language"), [("en", "English"), ("ja", "日本語")]
)
def test_summarize_declares_required_file_and_default_language(
    language: str, default_language: str
) -> None:
    path = (
        Path("guildbotics/editions/simple/templates/sample_commands")
        / language
        / "summarize.md"
    )

    metadata = load_markdown_with_frontmatter(path)

    assert metadata["args"] == {
        "file": {"required": True},
        "language": {"default": default_language},
    }
