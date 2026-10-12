"""Provider spelling belongs to provider implementations, not record keys."""

import ast
from pathlib import Path


def test_attribute_keys_and_event_types_have_no_provider_namespace():
    root = Path(__file__).parents[3] / "guildbotics"
    violations = []
    for path in root.rglob("*.py"):
        if path.name == "_version.py":
            continue  # Ignored build output, not a source of record keys.
        relative = path.relative_to(root)
        if relative.parts[0] == "integrations" and len(relative.parts) > 2:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            # Host names are network destinations, not attributes or event types.
            value = node.value
            if value.startswith(("github.", "slack.")) and value not in {
                "github.com",
                "slack.com",
            }:
                violations.append(f"{relative}:{node.lineno}: {value}")
    assert violations == []
