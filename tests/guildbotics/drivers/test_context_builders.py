"""Where a :class:`~guildbotics.runtime.context.Context` is built.

The host builds every context it runs with in ``drivers/context.py``; the
only other place is the entry inside a command's isolated environment. A
clone is derived from a context, so it is not a third place.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import guildbotics
from guildbotics.commands.errors import PersonExecutionNotAllowedError
from guildbotics.drivers import context as context_module
from guildbotics.drivers.context import resolve_member_context
from guildbotics.entities.team import Person, Project, Team

_ROOT = Path(guildbotics.__file__).parent
_CONTEXT_MODULES = {"guildbotics.runtime", "guildbotics.runtime.context"}

#: Every function that builds a context, by module and qualified name.
_BUILDERS = {
    ("drivers/context.py", "create_context"),
    ("runtime/command_entry.py", "run"),
    ("runtime/context.py", "Context.clone_for"),
}


def _context_names(tree: ast.Module, defines_context: bool) -> tuple[set, set]:
    """The names a module calls ``Context`` by, and the modules it reaches
    ``Context`` through as an attribute."""
    names = {"Context"} if defines_context else set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _CONTEXT_MODULES:
            names |= {a.asname or a.name for a in node.names if a.name == "Context"}
        elif isinstance(node, ast.ImportFrom) and node.module == "guildbotics.runtime":
            modules |= {a.asname or a.name for a in node.names if a.name == "context"}
        elif isinstance(node, ast.ImportFrom) and node.module == "guildbotics":
            modules |= {a.asname or a.name for a in node.names if a.name == "runtime"}
        elif isinstance(node, ast.Import):
            modules |= {
                a.asname or a.name for a in node.names if a.name in _CONTEXT_MODULES
            }
    return names, modules


def _builders(path: Path) -> set[tuple[str, str]]:
    relative = path.relative_to(_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names, modules = _context_names(tree, relative == "runtime/context.py")
    found: set[tuple[str, str]] = set()

    def is_context(node: ast.expr) -> bool:
        """Whether ``node`` names ``Context``, by a name or a module's
        attribute (a subclass builds one too)."""
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute)
            and node.attr == "Context"
            and ast.unparse(node.value) in modules
        )

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = [*scope, child.name]
            if isinstance(child, ast.Call) and is_context(child.func):
                found.add((relative, ".".join(scope) or "<module>"))
            if isinstance(child, ast.ClassDef) and any(map(is_context, child.bases)):
                found.add((relative, child.name))
            visit(child, inner)

    visit(tree, [])
    return found


def test_a_context_is_built_only_by_the_host_builder_and_the_environment_entry():
    found = set().union(*(_builders(path) for path in _ROOT.rglob("*.py")))

    assert found == _BUILDERS


class FakeContext:
    def __init__(self, team, person=None):
        self.team = team
        self.person = person

    def clone_for(self, person):
        return FakeContext(self.team, person)


def _use_team(monkeypatch, *members):
    team = Team(project=Project(name="demo"), members=list(members))

    def create_context(message: str = ""):
        return FakeContext(team)

    monkeypatch.setattr(context_module, "create_context", create_context)


def test_resolve_member_context_rejects_human_member(monkeypatch):
    _use_team(
        monkeypatch,
        Person(person_id="aiko", name="Aiko", person_type="agent"),
        Person(person_id="hana", name="Hana", person_type="human"),
    )

    with pytest.raises(PersonExecutionNotAllowedError):
        resolve_member_context("hana")


def test_resolve_member_context_clones_for_agent_member(monkeypatch):
    _use_team(
        monkeypatch,
        Person(person_id="aiko", name="Aiko", person_type="agent"),
    )

    context, person = resolve_member_context("Aiko")

    assert person.person_id == "aiko"
    assert context.person is person
