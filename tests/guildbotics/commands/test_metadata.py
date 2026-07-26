"""Unit tests for command metadata parsing (inputs / arguments / placeholders)."""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import (
    CommandInputPolicy,
    extract_placeholders,
    load_command_metadata,
    parse_command_arguments,
    parse_command_input_policy,
    parse_metadata_arguments,
    parse_python_arguments_from_source,
)


def test_parse_input_policy_defaults() -> None:
    assert parse_command_input_policy(None) == CommandInputPolicy()


def test_parse_input_policy_reads_declared_values() -> None:
    policy = parse_command_input_policy(
        {"defined_args": "hidden", "extra_args": "optional", "message": "required"}
    )
    assert policy == CommandInputPolicy(
        defined_args="hidden", extra_args="optional", message="required"
    )


def test_parse_input_policy_rejects_unknown_field() -> None:
    with pytest.raises(CommandError):
        parse_command_input_policy({"unexpected": "auto"})


def test_parse_input_policy_rejects_invalid_value() -> None:
    with pytest.raises(CommandError):
        parse_command_input_policy({"message": "sometimes"})


def test_parse_input_policy_rejects_non_mapping() -> None:
    with pytest.raises(CommandError):
        parse_command_input_policy(["message"])


def test_metadata_arguments_declared_and_discovered() -> None:
    metadata = {
        "args": {"file": {"required": True}, "language": {"default": "English"}},
        "body": "Translate ${file} into ${language} and ${extra}.",
    }
    arguments = {arg.name: arg for arg in parse_metadata_arguments(metadata)}

    assert arguments["file"].required is True
    assert arguments["language"].default == "English"
    assert arguments["language"].required is False
    # Undeclared placeholder becomes a discovered required keyword argument.
    assert arguments["extra"].kind == "keyword"
    assert arguments["extra"].required is True


def test_extract_placeholders_supports_all_syntaxes() -> None:
    metadata = {
        "body": "${one} {{ two }} {three} ${context} {{ now }}",
    }
    assert extract_placeholders(metadata) == {"one", "two", "three"}


def test_python_arguments_from_signature() -> None:
    source = (
        "def main(context, path, language='English', *, verbose=False):\n"
        "    return ''\n"
    )
    arguments = {arg.name: arg for arg in parse_python_arguments_from_source(source)}

    assert "context" not in arguments
    assert arguments["path"].kind == "positional"
    assert arguments["path"].required is True
    assert arguments["language"].default == "English"
    assert arguments["verbose"].kind == "keyword"
    assert arguments["verbose"].required is False


def test_python_arguments_ignore_syntax_error() -> None:
    assert parse_python_arguments_from_source("def main(:\n    pass") == []


def test_parse_command_arguments_dispatches_on_extension(tmp_path: Path) -> None:
    py_path = tmp_path / "task.py"
    py_path.write_text("def main(context, name):\n    return ''\n", encoding="utf-8")

    py_args = [arg.name for arg in parse_command_arguments(py_path, {})]
    md_args = [
        arg.name
        for arg in parse_command_arguments(
            tmp_path / "task.md", {"body": "Hello ${name}."}
        )
    ]

    assert py_args == ["name"]
    assert md_args == ["name"]


def test_load_command_metadata_reads_embedded_python_metadata(tmp_path: Path) -> None:
    command = tmp_path / "report.py"
    command.write_text(
        "COMMAND_METADATA = {\n"
        "    'name': 'Report',\n"
        "    'description': 'Reporting command.',\n"
        "}\n\n"
        'def main(context):\n    """Ignored docstring."""\n    return None\n',
        encoding="utf-8",
    )

    metadata = load_command_metadata(command, "en")

    assert metadata["name"] == "Report"
    assert metadata["description"] == "Reporting command."


def test_load_command_metadata_localizes_embedded_python_values(tmp_path: Path) -> None:
    command = tmp_path / "report.py"
    command.write_text(
        "COMMAND_METADATA = {'name': {'en': 'Report', 'ja': 'レポート'}}\n\n"
        "def main(context):\n    return None\n",
        encoding="utf-8",
    )

    metadata = load_command_metadata(command, "ja")

    assert metadata["name"] == "レポート"


def test_load_command_metadata_tolerates_broken_source(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yml"
    broken.write_text("::: not: valid: yaml:::\n- [", encoding="utf-8")

    assert load_command_metadata(broken, "en") == {}


def test_invalid_python_metadata_keeps_main_docstring_description(
    tmp_path: Path,
) -> None:
    command = tmp_path / "report.py"
    command.write_text(
        "COMMAND_METADATA = dict(name='Report')\n\n"
        "def main(context):\n"
        '    \"\"\"Fallback description.\"\"\"\n'
        "    return None\n",
        encoding="utf-8",
    )

    assert load_command_metadata(command, "en") == {
        "description": "Fallback description."
    }


def test_parse_python_arguments_tolerates_non_utf8_source(tmp_path: Path) -> None:
    command = tmp_path / "broken.py"
    command.write_bytes(b"\xff")

    assert parse_command_arguments(command, {}) == []
