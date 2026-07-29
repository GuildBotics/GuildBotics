"""Unit tests for format-specific command source validation."""

from __future__ import annotations

import shutil

import pytest

from guildbotics.commands.validation import (
    CommandValidationError,
    validate_command_source,
    validate_generated_command_source,
)


def test_markdown_without_frontmatter_is_valid() -> None:
    validate_command_source(".md", "Just a body with no frontmatter.")


def test_markdown_valid_frontmatter() -> None:
    validate_command_source(
        ".md",
        "---\nname: Task\ninputs:\n  message: required\n---\nBody.\n",
    )


def test_generated_markdown_requires_explicit_brain() -> None:
    source = "---\ninputs:\n  message: required\n---\nRewrite the input.\n"
    validate_command_source(".md", source)

    with pytest.raises(CommandValidationError) as exc:
        validate_generated_command_source(".md", source)

    assert "explicitly declare 'brain'" in str(exc.value)
    assert "non-empty string" in str(exc.value)


def test_generated_markdown_accepts_configured_custom_brain_name() -> None:
    source = "---\nbrain: translation\n---\nTranslate the input.\n"

    validate_generated_command_source(".md", source)


def test_generated_markdown_rejects_non_string_brain() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_generated_command_source(".md", "---\nbrain: []\n---\nBody.\n")

    assert "non-empty string" in str(exc.value)
    assert exc.value.context["reason"] == "generated_brain_invalid"


def test_markdown_invalid_frontmatter_yaml() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".md", "---\nname: [\n---\nBody.\n")
    assert exc.value.code == "command_file_invalid_source"


def test_markdown_invalid_inputs_contract() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(".md", "---\ninputs:\n  message: sometimes\n---\n")
    assert exc.value.code == "command_file_invalid_source"


def test_markdown_disabled_brain_cannot_require_unconsumed_message() -> None:
    source = (
        "---\n"
        "brain: none\n"
        "inputs:\n"
        "  message: required\n"
        "---\n"
        "Polish the supplied input text.\n"
    )

    validate_command_source(".md", source)

    with pytest.raises(CommandValidationError) as exc:
        validate_generated_command_source(".md", source)

    assert "brain: none" in str(exc.value)
    assert "brain: default" in str(exc.value)
    assert "input message" in str(exc.value)


def test_generated_markdown_disabled_brain_can_render_context_pipe() -> None:
    source = (
        "---\n"
        "brain: none\n"
        "template_engine: jinja2\n"
        "inputs:\n"
        "  message: required\n"
        "---\n"
        "Echo: {{ context.pipe }}\n"
    )

    validate_generated_command_source(".md", source)


def test_generated_markdown_rejects_whitespace_padded_disabled_brain() -> None:
    source = (
        "---\nbrain: ' disabled '\ninputs:\n  message: required\n---\n"
        "Unused input.\n"
    )

    with pytest.raises(CommandValidationError) as exc:
        validate_generated_command_source(".md", source)

    assert exc.value.context["reason"] == "required_message_not_consumed"


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
    validate_command_source(
        ".py",
        "COMMAND_METADATA = {'routine': True}\n\ndef main(context):\n    return ''\n",
    )


def test_python_metadata_must_be_static() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(
            ".py",
            "COMMAND_METADATA = dict(routine=True)\n\ndef main(context):\n    return ''\n",
        )
    assert "static literal mapping" in str(exc.value)


def test_python_metadata_validates_inputs() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(
            ".py",
            "COMMAND_METADATA = {'inputs': {'message': 'sometimes'}}\n\n"
            "def main(context):\n    return ''\n",
        )
    assert "inputs.message" in str(exc.value)


def test_python_metadata_validates_argument_declarations() -> None:
    with pytest.raises(CommandValidationError) as exc:
        validate_command_source(
            ".py",
            "COMMAND_METADATA = {'args': [{'name': 'text'}]}\n\n"
            "def main(context):\n    return ''\n",
        )

    assert "Command 'args' must be a mapping" in str(exc.value)


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
