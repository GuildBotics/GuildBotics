"""Keep literal command-key reads from duplicating entry normalization."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPOSITORY_ROOT / "guildbotics"

#: Direct reads that do not enumerate command metadata entries.
NON_ENTRY_COMMAND_READS = Counter(
    {
        (
            "guildbotics/app_api/hotkeys.py",
            "load_hotkeys",
            "get",
        ): 1,
        (
            "guildbotics/commands/validation.py",
            "_validate_generated_markdown_message_usage",
            "get:not",
        ): 1,
    }
)


def _enclosing_function(node: ast.AST) -> str:
    names: list[str] = []
    current: ast.AST | None = getattr(node, "parent", None)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(current.name)
        current = getattr(current, "parent", None)
    return ".".join(reversed(names)) or "<module>"


def _literal_commands_read_kind(node: ast.AST) -> str | None:
    """Classify direct reads whose key is the literal ``commands``."""
    if isinstance(node, ast.Subscript):
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "commands"
        ):
            return "subscript"
        return None
    if not isinstance(node, ast.Call):
        if isinstance(node, ast.MatchMapping) and any(
            isinstance(key, ast.Constant) and key.value == "commands"
            for key in node.keys
        ):
            return "mapping-pattern"
        return None
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "pop", "setdefault", "__getitem__"}
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "commands"
    ):
        if isinstance(node.parent, ast.UnaryOp) and isinstance(node.parent.op, ast.Not):
            return f"{node.func.attr}:not"
        return node.func.attr
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "getitem"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "commands"
    ):
        return "operator.getitem"
    return None


def _direct_command_reads() -> Counter[tuple[str, str, str]]:
    reads: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]
        for node in ast.walk(tree):
            kind = _literal_commands_read_kind(node)
            if kind is None:
                continue
            reads[
                (
                    path.relative_to(REPOSITORY_ROOT).as_posix(),
                    _enclosing_function(node),
                    kind,
                )
            ] += 1
    return reads


def test_literal_command_key_reads_keep_entry_enumeration_owned() -> None:
    """A new literal-key reader must choose shared normalization explicitly."""
    reads = _direct_command_reads()
    owner_reads = Counter(
        {
            site: count
            for site, count in reads.items()
            if site[:2] == ("guildbotics/commands/metadata.py", "command_entries")
        }
    )
    reads.subtract(owner_reads)
    reads += Counter()

    assert sum(owner_reads.values()) == 1
    assert reads == NON_ENTRY_COMMAND_READS
