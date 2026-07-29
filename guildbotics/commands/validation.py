"""Format-specific execution-readiness validation for command files.

Invalid drafts may be saved so users can preserve intermediate work. Validation
lives in the command domain because it reuses the same metadata parsing and
argument definition rules as execution. The App API calls it when determining
whether a saved command can run and blocks execution until the source is valid.
"""

from __future__ import annotations

import ast
import shlex
import shutil
import subprocess
from typing import Any

import yaml
from jinja2 import Environment, TemplateSyntaxError, nodes

from guildbotics.commands.arguments import parse_command_argument_definitions
from guildbotics.commands.brains import is_brain_disabled
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import (
    find_main_function,
    parse_command_input_policy,
    parse_python_metadata_from_module,
)
from guildbotics.commands.registry import get_command_types

SHELL_VALIDATION_TIMEOUT_SECONDS = 5


class CommandValidationError(CommandError):
    """A command source failed validation.

    Attributes:
        code: Stable error code shared with the App API and frontend.
        context: Extra fields (line/column/field/reason) for diagnostics.
    """

    def __init__(
        self, code: str, message: str, context: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context: dict[str, str] = dict(context or {})


def validate_command_source(extension: str, content: str) -> None:
    """Validate command source for the given file extension.

    Args:
        extension: File extension including the leading dot (e.g. ``".md"``).
        content: The full UTF-8 source text.

    Raises:
        CommandValidationError: If the extension is unsupported or the source is
            not a valid command definition.
    """
    ext = extension.lower()
    if ext == ".md":
        validate_markdown_source(content)
    elif ext in {".yaml", ".yml"}:
        validate_yaml_source(content)
    elif ext == ".py":
        validate_python_source(content)
    elif ext == ".sh":
        validate_shell_source(content)
    else:
        raise CommandValidationError(
            "command_file_unsupported_format",
            f"Unsupported command extension: {extension!r}.",
            {"extension": ext},
        )


def validate_generated_command_source(extension: str, content: str) -> None:
    """Validate source plus invariants required of AI-generated commands."""
    validate_command_source(extension, content)
    if extension.lower() != ".md":
        return
    front_matter, body = split_frontmatter(content)
    config = _load_frontmatter_mapping(front_matter) if front_matter.strip() else {}
    if "brain" not in config:
        raise CommandValidationError(
            "command_file_invalid_source",
            "AI-generated Markdown commands must explicitly declare 'brain' as "
            "a non-empty string.",
            {"reason": "generated_brain_not_explicit"},
        )
    brain = config["brain"]
    if not isinstance(brain, str) or not brain.strip():
        raise CommandValidationError(
            "command_file_invalid_source",
            "AI-generated Markdown commands must set 'brain' to a non-empty string.",
            {"reason": "generated_brain_invalid"},
        )
    _validate_generated_markdown_message_usage(config, body)


def validate_markdown_source(content: str) -> None:
    """Validate a Markdown command's frontmatter and declarations."""
    front_matter, _ = split_frontmatter(content)
    if not front_matter.strip():
        return
    config = _load_frontmatter_mapping(front_matter)
    _validate_declarations(config)


def validate_yaml_source(content: str) -> None:
    """Validate a YAML command definition."""
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _yaml_error(exc, "Command YAML is not valid.") from exc
    if loaded is None:
        raise CommandValidationError(
            "command_file_invalid_source",
            "Command YAML must define a mapping.",
        )
    if not isinstance(loaded, dict):
        raise CommandValidationError(
            "command_file_invalid_source",
            "Command YAML root must be a mapping.",
        )
    _validate_declarations(loaded)


def validate_python_source(content: str) -> None:
    """Validate a Python command has a parseable, ``main``-defining module."""
    try:
        module = ast.parse(content)
    except SyntaxError as exc:
        context: dict[str, str] = {}
        if exc.lineno is not None:
            context["line"] = str(exc.lineno)
        if exc.offset is not None:
            context["column"] = str(exc.offset)
        raise CommandValidationError(
            "command_file_invalid_source",
            f"Python command has a syntax error: {exc.msg}.",
            context,
        ) from exc
    if find_main_function(module) is None:
        raise CommandValidationError(
            "command_file_invalid_source",
            "Python command must define a top-level 'main' function.",
        )
    try:
        metadata = parse_python_metadata_from_module(module)
        parse_command_input_policy(metadata.get("inputs"))
        parse_command_argument_definitions(metadata)
    except CommandError as exc:
        raise CommandValidationError("command_file_invalid_source", str(exc)) from exc


def validate_shell_source(content: str) -> None:
    """Validate a shell command with ``bash -n`` without executing it."""
    if shutil.which("bash") is None:
        raise CommandValidationError(
            "command_file_invalid_source",
            "Shell command validation requires 'bash', which is unavailable.",
            {"reason": "shell_validator_unavailable"},
        )
    try:
        result = subprocess.run(
            ["bash", "-n"],
            input=content,
            text=True,
            capture_output=True,
            timeout=SHELL_VALIDATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandValidationError(
            "command_file_invalid_source",
            "Shell command validation timed out.",
            {"reason": "shell_validator_timeout"},
        ) from exc
    if result.returncode != 0:
        raise CommandValidationError(
            "command_file_invalid_source",
            "Shell command has a syntax error.",
            {"detail": result.stderr.strip()[:500]},
        )


def split_frontmatter(content: str) -> tuple[str, str]:
    """Split a Markdown source into ``(frontmatter, body)`` text.

    When no leading ``---`` frontmatter block is present, the frontmatter part
    is empty and the body holds the entire content.
    """
    if not content.startswith("---"):
        return "", content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip("\r\n") != "---":
        return "", content
    front_lines: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip("\r\n") == "---":
            body = "".join(lines[index + 1 :])
            return "".join(front_lines), body
        front_lines.append(line)
    return "", content


def _load_frontmatter_mapping(front_matter: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise _yaml_error(exc, "Command frontmatter is not valid YAML.") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise CommandValidationError(
            "command_file_invalid_source",
            "Command frontmatter must be a mapping.",
        )
    return loaded


def _validate_declarations(config: dict[str, Any]) -> None:
    try:
        parse_command_input_policy(config.get("inputs"))
        parse_command_argument_definitions(config)
    except CommandError as exc:
        raise CommandValidationError("command_file_invalid_source", str(exc)) from exc
    _validate_commands_shape(config)


def _validate_generated_markdown_message_usage(
    config: dict[str, Any], body: str
) -> None:
    inputs = config.get("inputs")
    message_required = isinstance(inputs, dict) and inputs.get("message") == "required"
    if (
        message_required
        and is_brain_disabled(config.get("brain", ""))
        and not config.get("commands")
        and not _jinja_consumes_context_pipe(config, body)
    ):
        raise CommandValidationError(
            "command_file_invalid_source",
            "Markdown command declares a required input message, but brain: none "
            "does not consume it and no child command can do so. Set "
            "'brain: default' to use the configured LLM or add an input-consuming "
            "child command.",
            {"reason": "required_message_not_consumed"},
        )


def _jinja_consumes_context_pipe(config: dict[str, Any], body: str) -> bool:
    if config.get("template_engine") != "jinja2":
        return False
    try:
        parsed = Environment(autoescape=False).parse(body)
    except TemplateSyntaxError:
        return False
    if any(
        isinstance(node.node, nodes.Name)
        and node.node.name == "context"
        and node.attr == "pipe"
        for node in parsed.find_all(nodes.Getattr)
    ):
        return True
    return any(
        isinstance(node.node, nodes.Name)
        and node.node.name == "context"
        and isinstance(node.arg, nodes.Const)
        and node.arg.value == "pipe"
        for node in parsed.find_all(nodes.Getitem)
    )


def _validate_commands_shape(config: dict[str, Any]) -> None:
    raw = config.get("commands")
    if raw is None:
        return
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    for entry in entries:
        _validate_command_entry(entry)


def _validate_command_entry(entry: Any) -> None:
    if isinstance(entry, dict):
        # Mirror the spec factory's acceptance so a source that saves cannot
        # break at runtime: the entry must be an inline command (a registered
        # inline key) or reference another command via command/path/name.
        if any(
            command_type.is_inline_command(entry)
            for command_type in get_command_types()
        ):
            return
        if any(key in entry for key in ("command", "path", "name")):
            return
        raise CommandValidationError(
            "command_file_invalid_source",
            "Command entry requires 'command', 'path', 'name' or an inline command "
            "key (e.g. 'prompt', 'python', 'script', 'print', 'to_html', 'to_pdf').",
        )
    if isinstance(entry, str):
        try:
            words = shlex.split(entry)
        except ValueError as exc:
            raise CommandValidationError(
                "command_file_invalid_source",
                f"Command entry could not be parsed: {exc}.",
            ) from exc
        if not words:
            raise CommandValidationError(
                "command_file_invalid_source",
                "Command entry string cannot be empty.",
            )
        return
    raise CommandValidationError(
        "command_file_invalid_source",
        "Command entry must be a mapping or string.",
    )


def _yaml_error(exc: yaml.YAMLError, message: str) -> CommandValidationError:
    context: dict[str, str] = {}
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        context["line"] = str(mark.line + 1)
        context["column"] = str(mark.column + 1)
    return CommandValidationError("command_file_invalid_source", message, context)
