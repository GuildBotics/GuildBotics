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
from guildbotics.environment.inference_host import DirectInference
from guildbotics.intelligences.brains.inference import inference

_ROOT = Path(guildbotics.__file__).parent

#: Every function that builds a context, by module and qualified name.
_BUILDERS = {
    ("drivers/context.py", "create_context"),
    ("guest/entry.py", "run"),
    ("runtime/context.py", "Context.clone_for"),
}

#: The modules whose ``Context`` is not GuildBotics' (``contextvars.Context``
#: runs a call in a copy of the context variables).
_OTHER_CONTEXTS = {"contextvars"}


def _builders(source: str, relative: str) -> set[tuple[str, str]]:
    """The functions in ``source`` that call or subclass ``Context``.

    A context is told by its name alone, ``Context`` or any name it is
    imported as, called bare or as any module's attribute, so no form of
    import hides one. Another class named ``Context`` is counted too, unless
    it is reached through one of :data:`_OTHER_CONTEXTS`.
    """
    tree = ast.parse(source)
    names = {"Context"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "Context" and alias.asname
    }

    def is_context(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute)
            and node.attr == "Context"
            and ast.unparse(node.value) not in _OTHER_CONTEXTS
        )

    found: set[tuple[str, str]] = set()

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
    found = set().union(
        *(
            _builders(
                path.read_text(encoding="utf-8"), path.relative_to(_ROOT).as_posix()
            )
            for path in _ROOT.rglob("*.py")
        )
    )

    assert found == _BUILDERS


@pytest.mark.parametrize(
    "source",
    [
        "from guildbotics.runtime import Context\ndef build(): Context(1, 2)",
        "from guildbotics.runtime.context import Context as C\ndef build(): C(1, 2)",
        "from guildbotics.runtime import context\ndef build(): context.Context(1, 2)",
        "from guildbotics.runtime import context as rt\ndef build(): rt.Context(1, 2)",
        "import guildbotics.runtime.context\n"
        "def build(): guildbotics.runtime.context.Context(1, 2)",
        "from guildbotics import runtime\ndef build(): runtime.Context(1, 2)",
    ],
)
def test_a_context_is_found_however_it_is_imported(source):
    assert _builders(source, "m.py") == {("m.py", "build")}


def test_a_subclass_of_context_is_found():
    source = "from guildbotics.runtime import context\nclass Mine(context.Context): ..."

    assert _builders(source, "m.py") == {("m.py", "Mine")}


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


def test_no_way_to_the_inference_apis_is_assumed() -> None:
    """Only the place that builds the process's context says where a brain's
    inference call goes."""
    with pytest.raises(RuntimeError, match="No inference implementation"):
        inference()


def test_the_host_builder_has_its_brains_call_the_apis_themselves(
    configured_team,
) -> None:
    configured_team.team = Team(project=Project(name="demo"), members=[])

    context_module.create_context()

    assert isinstance(inference(), DirectInference)
