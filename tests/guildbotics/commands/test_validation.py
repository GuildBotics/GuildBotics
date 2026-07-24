"""Unit tests for format-specific command source validation."""

from __future__ import annotations

import shutil

import pytest

from guildbotics.commands.validation import (
    CommandValidationError,
    validate_command_source,
)


def test_markdown_without_frontmatter_is_valid() -> None:
    validate_command_source(".md", "Just a body with no frontmatter.")


def test_markdown_valid_frontmatter() -> None:
    validate_command_source(
        ".md",
        "---\nname: Task\ninputs:\n  message: required\n---\nBody.\n",
    )


def test_markdown_invalid_frontmatter_yaml() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".md", "---\nname: [\n---\nBody.\n")
    assert exc.value.code == "command_file_invalid_source"


def test_markdown_invalid_inputs_contract() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".md", "---\ninputs:\n  message: sometimes\n---\n")
    assert exc.value.code == "command_file_invalid_source"


def test_markdown_frontmatter_must_be_mapping() -> None:
    with pytest.raises(CommandValidationError):
        validate_command_source(".md", "---\n- just\n- a\n- list\n---\n")


def test_yaml_valid() -> None:
    validate_command_source(".yaml", "commands: []\n")


def test_yaml_root_must_be_mapping() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".yaml", "- one\n- two\n")
    assert exc.value.code == "command_file_invalid_source"


def test_yaml_invalid_syntax_reports_line() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".yml", "commands: [\n")
    assert exc.value.code == "command_file_invalid_source"
    assert "line" in exc.value.context


def test_yaml_commands_entry_shape_invalid() -> None:
    with pytest.raises(CommandValidationError):
        validate_command_source(".yaml", "commands:\n  - 123\n")


def test_yaml_dict_entry_without_recognized_key_invalid() -> None:
    # A dict child command that the runtime spec factory would reject (no
    # command/path/name and no inline key) must not save.
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".yaml", "commands:\n  - foo: bar\n")
    assert exc.value.code == "command_file_invalid_source"


def test_yaml_dict_entry_with_command_or_inline_key_valid() -> None:
    validate_command_source(".yaml", "commands:\n  - command: functions/get_time\n")
    validate_command_source(".yaml", "commands:\n  - prompt: Say hello.\n")


def test_python_valid() -> None:
    validate_command_source(".py", "def main(context):\n    return ''\n")


def test_python_syntax_error() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".py", "def main(:\n    pass\n")
    assert exc.value.code == "command_file_invalid_source"
    assert "line" in exc.value.context


def test_python_missing_main() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".py", "def helper():\n    return ''\n")
    assert exc.value.code == "command_file_invalid_source"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_shell_valid() -> None:
    validate_command_source(".sh", "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_shell_syntax_error() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".sh", "if true; then\n")
    assert exc.value.code == "command_file_invalid_source"


def test_shell_without_bash_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("guildbotics.commands.validation.shutil.which", lambda _: None)
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".sh", "echo ok\n")
    assert exc.value.context.get("reason") == "shell_validator_unavailable"


def test_unsupported_format() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".txt", "anything")
    assert exc.value.code == "command_file_unsupported_format"
