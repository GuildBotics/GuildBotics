"""The package map holds: what runs inside a command's isolated environment
imports nothing only the host may hold, and every shared package is used
there.

Import-linter checks that shared code never imports guest implementation.
The package population and dynamic imports are checked here by driving the
guest entry, named brains, bundled commands, and their response classes.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import grimp
import yaml

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_SHARED = tuple(
    "guildbotics." + name
    for name in ("commands", "intelligences", "runtime", "loader", "entities", "utils")
)
_ALLOWED = ("guildbotics.guest", *_SHARED)
_HOST_ONLY = {"fastapi", "keyring", "microsandbox", "uvicorn"}
_TEMPLATES = Path(__file__).resolve().parents[2] / "guildbotics" / "templates"
_BRAIN_MAPPING = _TEMPLATES / "intelligences" / "brain_mapping.yml"

#: Loads the environment's entry, every brain the bundled mapping names, every
#: adapter, every module of the machinery, every bundled Python command the way
#: the machinery loads one, and every response class a bundled prompt names.
_PROBE = """
import importlib, json, pkgutil, sys
from pathlib import Path
import guildbotics.commands as package
from guildbotics.commands.python_command import _load_python_module
import guildbotics.guest.entry
import guildbotics.guest.factory
from guildbotics.utils.fileio import (
    get_template_path,
    load_markdown_with_frontmatter,
    load_yaml_file,
)
from guildbotics.utils.import_utils import ClassResolver
for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
templates = get_template_path()
mapping = load_yaml_file(templates / "intelligences" / "brain_mapping.yml")
for slot in mapping.values():
    importlib.import_module({"model":"guildbotics.intelligences.brains.agno_agent", "ai_cli":"guildbotics.guest.cli_agent", "jev":"guildbotics.intelligences.brains.jev"}[slot["kind"]])
for path in sorted((templates / "commands").rglob("*.py")):
    _load_python_module(path)
for path in sorted((templates / "commands").rglob("*.md")):
    config = load_markdown_with_frontmatter(path)
    if config.get("response_class"):
        ClassResolver(config.get("schema", ""), None).get_model_class(
            config["response_class"]
        )
print(json.dumps(sorted(sys.modules)))
"""

#: Drives a completion-managed invocation against a stub ledger: the first
#: attempt ends without completion, the second completes. The ledger is the
#: only way to the run record, so no host module is needed at run time either.
_TURN_PROBE = """
import asyncio, json, sys
from guildbotics.commands.runner import CommandRunner

calls = []

class Ledger:
    run_id = "run-1"

    def require_completion(self):
        calls.append("require_completion")
        if calls.count("require_completion") == 1:
            raise RuntimeError("not completed")

    def evidence(self):
        calls.append("evidence")
        return []

    def record_completed(self, attempt):
        calls.append("record_completed")

    def record_completion_missing(self, attempt, max_attempts, error):
        calls.append("record_completion_missing")

async def invoke_once(name, args, kwargs, cwd):
    return "response"

runner = CommandRunner.__new__(CommandRunner)
runner._ledger = Ledger()
runner._invoke_once = invoke_once
result = asyncio.run(
    runner._invoke(
        "functions/handle_chat_event",
        agent_execution_context={"max_completion_attempts": 2, "event_id": "E1"},
    )
)
print(json.dumps({"result": result, "calls": calls, "loaded": sorted(sys.modules)}))
"""


def _run(probe: str) -> object:
    return json.loads(
        subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )


def _host_only(loaded: list[str]) -> list[str]:
    return [
        name
        for name in loaded
        if name.startswith("guildbotics.")
        and not any(
            name == allowed or name.startswith(allowed + ".") for allowed in _ALLOWED
        )
    ] + sorted({name.split(".")[0] for name in loaded} & _HOST_ONLY)


def test_what_runs_in_the_environment_loads_nothing_host_only() -> None:
    loaded = _run(_PROBE)

    assert isinstance(loaded, list)
    assert {
        "guildbotics.commands.runner",
        "guildbotics.guest.cli_agent",
        "guildbotics.intelligences.brains.factory",
        "guildbotics.guest.codex",
    } <= set(loaded)
    assert _host_only(loaded) == []


def test_the_bundled_python_commands_import_nothing_host_only() -> None:
    """import-linter does not see the bundled commands, loaded by path, and the
    probe runs only what loading them runs: an import inside a function is
    read here."""
    imported: list[str] = []
    for path in sorted(_TEMPLATES.joinpath("commands").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                # ``from guildbotics import sync`` names the package in the alias.
                imported.extend(f"{node.module}.{alias.name}" for alias in node.names)

    assert "guildbotics.commands.errors.CommandError" in imported
    assert _host_only(imported) == []


def test_a_completion_managed_turn_reaches_the_run_record_only_through_the_ledger() -> (
    None
):
    outcome = _run(_TURN_PROBE)

    assert isinstance(outcome, dict)
    assert outcome["result"] == "response"
    assert outcome["calls"] == [
        "evidence",
        "require_completion",
        "record_completion_missing",
        "evidence",
        "require_completion",
        "record_completed",
    ]
    assert [
        name for name in outcome["loaded"] if name.startswith("guildbotics.drivers")
    ] == []


def _unreached_shared_packages(graph: grimp.ImportGraph) -> list[str]:
    """The shared packages and subpackages none of whose modules the guest
    reaches from its entries: the entry the host starts, the working
    directory's copy, and the brains the bundled mapping names (loaded by
    name, so no import reaches them), and bundled commands loaded by path."""
    mapping = yaml.safe_load(_BRAIN_MAPPING.read_text(encoding="utf-8"))
    roots = {"guildbotics.guest.entry", "guildbotics.guest.worktree_copy"} | {
        {
            "model": "guildbotics.intelligences.brains.agno_agent",
            "ai_cli": "guildbotics.guest.cli_agent",
            "jev": "guildbotics.intelligences.brains.jev",
        }[slot["kind"]]
        for slot in mapping.values()
    }
    # Commands are loaded by file path, outside grimp's package graph. Their
    # imports, including lazy imports inside main(), are real VM entry edges.
    for path in (_TEMPLATES / "commands").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            roots.update(name for name in names if name in graph.modules)
    reached = set(roots).union(*(graph.find_upstream_modules(root) for root in roots))
    packages = sorted(
        name
        for name in graph.modules
        if any(name == shared or name.startswith(shared + ".") for shared in _SHARED)
        and _is_package(name)
    )
    return [
        package
        for package in packages
        if not any(
            module == package or module.startswith(package + ".") for module in reached
        )
    ]


def _is_package(name: str) -> bool:
    spec = importlib.util.find_spec(name)
    return spec is not None and spec.submodule_search_locations is not None


def test_every_shared_package_is_used_inside_the_environment() -> None:
    """A package the guest never reaches is the host's, misfiled as shared."""
    graph = grimp.build_graph("guildbotics")

    assert _unreached_shared_packages(graph) == []


def test_a_shared_package_the_guest_does_not_reach_is_reported() -> None:
    graph = grimp.build_graph("guildbotics")
    loader = {name for name in graph.modules if name.startswith("guildbotics.loader")}
    for module in loader:
        for importer in graph.find_modules_that_directly_import(module) - loader:
            graph.remove_import(importer=importer, imported=module)

    assert _unreached_shared_packages(graph) == [
        "guildbotics.loader",
        "guildbotics.loader.yaml",
    ]


def test_python_package_contains_no_host_entries():
    root = _PYPROJECT.parent / "guildbotics"
    host = "app_api cli drivers setup environment capabilities integrations workspace observability local_git".split()
    assert not [name for name in host if (root / name).exists()]
    assert (
        "scripts"
        not in tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    )
