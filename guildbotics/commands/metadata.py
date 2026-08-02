"""Command metadata domain models and parsing.

This module owns the command-domain knowledge for interpreting command source
metadata: the ``inputs`` policy, declared/discovered argument metadata, Python
``main`` signatures and placeholder discovery. It depends only on the command
package and shared utilities, never on the App API wire models, so that both the
runtime catalog and source validation can reuse the same parsing.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from guildbotics.commands.arguments import parse_command_argument_definitions
from guildbotics.commands.errors import CommandError
from guildbotics.utils.fileio import (
    load_markdown_with_frontmatter,
    load_yaml_file,
)

DEFINED_ARGS_VALUES = ("auto", "hidden")
EXTRA_ARGS_VALUES = ("hidden", "optional")
MESSAGE_VALUES = ("hidden", "optional", "required")

_PLACEHOLDER_PATTERNS = (
    re.compile(r"\$\{\s*([A-Za-z_]\w*|\d+)\s*\}"),
    re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}"),
    re.compile(r"(?<![\{$])\{([A-Za-z_]\w*)\}(?!\})"),
)
_RESERVED_PLACEHOLDERS = {"context", "now", "effort"}
PYTHON_COMMAND_METADATA_NAME = "COMMAND_METADATA"


@dataclass(frozen=True)
class CommandInputPolicy:
    """How a command accepts caller-supplied input.

    Mirrors the ``inputs`` frontmatter/metadata block. This is the domain model
    used by the runtime catalog and validation; the App API converts it to its
    wire ``CommandInputs`` model.
    """

    defined_args: str = "auto"
    extra_args: str = "hidden"
    message: str = "optional"


@dataclass(frozen=True)
class CommandArgumentMetadata:
    """A single caller-visible command argument."""

    name: str
    kind: str  # "positional" | "keyword"
    required: bool = False
    default: str = ""


def default_command_label(command: str) -> str:
    """Derive a human-readable label from a logical command name."""
    return command.rsplit("/", 1)[-1].replace("_", " ").replace("-", " ").title()


def parse_command_input_policy(value: Any) -> CommandInputPolicy:
    """Parse and validate an ``inputs`` metadata block.

    Args:
        value: The raw ``inputs`` value from command metadata.

    Returns:
        The validated input policy.

    Raises:
        CommandError: If the block or any field is invalid.
    """
    if value is None:
        return CommandInputPolicy()
    if not isinstance(value, dict):
        raise CommandError("Command 'inputs' must be a mapping.")

    allowed = {"defined_args", "extra_args", "message"}
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise CommandError(f"Command 'inputs' has unsupported fields: {names}.")

    defined_args = _validate_choice(value, "defined_args", DEFINED_ARGS_VALUES, "auto")
    extra_args = _validate_choice(value, "extra_args", EXTRA_ARGS_VALUES, "hidden")
    message = _validate_choice(value, "message", MESSAGE_VALUES, "optional")
    return CommandInputPolicy(
        defined_args=defined_args,
        extra_args=extra_args,
        message=message,
    )


def _validate_choice(
    mapping: dict[str, Any], key: str, choices: tuple[str, ...], default: str
) -> str:
    if key not in mapping:
        return default
    value = mapping[key]
    if value not in choices:
        allowed = ", ".join(choices)
        raise CommandError(f"Command 'inputs.{key}' must be one of: {allowed}.")
    return str(value)


def parse_command_arguments(
    path: Path | None, metadata: dict[str, Any]
) -> list[CommandArgumentMetadata]:
    """Return caller-visible arguments for a command.

    Python commands derive arguments from the ``main`` signature; Markdown and
    YAML commands derive them from declared ``args`` plus placeholder discovery.
    """
    if path is not None and path.suffix == ".py":
        return parse_python_arguments_from_source(_safe_read_text(path))
    return parse_metadata_arguments(metadata)


def parse_metadata_arguments(
    metadata: dict[str, Any],
) -> list[CommandArgumentMetadata]:
    """Parse declared and placeholder-discovered arguments from metadata."""
    placeholders = extract_placeholders(metadata)
    definitions = parse_command_argument_definitions(metadata)
    declared_names = {definition.name for definition in definitions}
    positional = sorted(
        int(name)
        for name in placeholders - declared_names
        if name.isdigit() and int(name) > 0
    )
    keywords = sorted(
        name for name in placeholders - declared_names if not name.isdigit()
    )
    declared = [
        CommandArgumentMetadata(
            name=definition.name,
            kind="positional" if definition.name.isdigit() else "keyword",
            required=definition.required,
            default=definition.default or "",
        )
        for definition in definitions
    ]
    discovered = [
        CommandArgumentMetadata(name=str(index), kind="positional", required=True)
        for index in positional
    ] + [
        CommandArgumentMetadata(name=name, kind="keyword", required=True)
        for name in keywords
    ]
    return declared + discovered


def extract_placeholders(metadata: dict[str, Any]) -> set[str]:
    """Discover ``${x}`` / ``{{ x }}`` / ``{x}`` placeholders in command text."""
    text = "\n".join(
        str(value)
        for key, value in metadata.items()
        if key in {"body", "description"} and value
    )
    names: set[str] = set()
    for pattern in _PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(text):
            names.add(match.group(1))
    return names - _RESERVED_PLACEHOLDERS


def parse_python_arguments_from_source(source: str) -> list[CommandArgumentMetadata]:
    """Extract caller-visible arguments from a Python command ``main`` signature."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []
    main = find_main_function(module)
    if main is None:
        return []

    args: list[CommandArgumentMetadata] = []
    positional = list(main.args.posonlyargs) + list(main.args.args)
    defaults = [None] * (len(positional) - len(main.args.defaults)) + list(
        main.args.defaults
    )
    for index, arg in enumerate(positional):
        if index == 0 and arg.arg in {"context", "ctx", "c"}:
            continue
        default = defaults[index]
        args.append(
            CommandArgumentMetadata(
                name=arg.arg,
                kind="positional",
                required=default is None,
                default=literal_default(default),
            )
        )

    keyword_defaults = dict(
        zip(main.args.kwonlyargs, main.args.kw_defaults, strict=True)
    )
    for arg, default in keyword_defaults.items():
        args.append(
            CommandArgumentMetadata(
                name=arg.arg,
                kind="keyword",
                required=default is None,
                default=literal_default(default),
            )
        )
    return args


def find_main_function(
    module: ast.Module,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the top-level ``main`` function definition, if any."""
    for node in module.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "main"
        ):
            return node
    return None


def literal_default(node: ast.expr | None) -> str:
    """Render a default-argument AST node as its string value."""
    if node is None:
        return ""
    value = ast.literal_eval(node) if isinstance(node, ast.Constant) else None
    if value is None:
        return ""
    return str(value)


def load_command_metadata(path: Path, language_code: str = "") -> dict[str, Any]:
    """Load and localize metadata embedded in a command file."""
    return _localize_metadata(load_command_file_metadata(path), language_code)


def load_command_file_metadata(path: Path) -> dict[str, Any]:
    """Load metadata embedded in the command file itself."""
    try:
        if path.suffix == ".md":
            data = load_markdown_with_frontmatter(path)
            return data if isinstance(data, dict) else {}
        if path.suffix in {".yml", ".yaml"}:
            loaded = load_yaml_file(path)
            return loaded if isinstance(loaded, dict) else {}
        if path.suffix == ".py":
            module = ast.parse(path.read_text(encoding="utf-8"))
            main = find_main_function(module)
            description = ast.get_docstring(main) if main is not None else ""
            try:
                metadata = parse_python_metadata_from_module(module)
            except CommandError:
                metadata = {}
            if "description" not in metadata:
                metadata["description"] = _first_line(description or "")
            return metadata
    except (CommandError, OSError, SyntaxError, ValueError, yaml.YAMLError):
        return {}
    return {}


def parse_python_metadata_from_module(module: ast.Module) -> dict[str, Any]:
    """Read the static ``COMMAND_METADATA`` mapping from a Python module AST.

    Metadata discovery must never import a command module because its top-level
    code may have side effects. Only a literal mapping is accepted so catalog
    reads and editor validation remain deterministic.

    Args:
        module: Parsed Python module.

    Returns:
        The declared metadata, or an empty mapping when none is declared.

    Raises:
        CommandError: If the declaration is duplicated, dynamic, or not a
            string-keyed mapping.
    """
    declarations = [
        value
        for node in module.body
        if (value := _assigned_metadata_value(node)) is not None
    ]
    if not declarations:
        return {}
    if len(declarations) > 1:
        raise CommandError(
            f"Python command must declare {PYTHON_COMMAND_METADATA_NAME} at most once."
        )
    try:
        value = ast.literal_eval(declarations[0])
    except (ValueError, TypeError, MemoryError, RecursionError) as exc:
        raise CommandError(
            f"Python command {PYTHON_COMMAND_METADATA_NAME} must be a static literal mapping."
        ) from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CommandError(
            f"Python command {PYTHON_COMMAND_METADATA_NAME} must be a string-keyed mapping."
        )
    return value


def _assigned_metadata_value(node: ast.stmt) -> ast.expr | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    else:
        return None
    if isinstance(target, ast.Name) and target.id == PYTHON_COMMAND_METADATA_NAME:
        return node.value
    return None


def _localize_metadata(metadata: dict[str, Any], language_code: str) -> dict[str, Any]:
    localized = dict(metadata)
    for key in ("name", "description"):
        localized[key] = _localized_metadata_value(localized.get(key), language_code)
    return {key: value for key, value in localized.items() if value is not None}


def _localized_metadata_value(value: Any, language_code: str) -> Any:
    if not isinstance(value, dict):
        return value
    if language_code and language_code in value:
        return value[language_code]
    if "en" in value:
        return value["en"]
    return next(iter(value.values()), None)


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
