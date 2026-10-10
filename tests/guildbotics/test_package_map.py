"""The package map holds: what runs inside a command's isolated environment
imports nothing only the host may hold, and every shared package is used
there.

The map is the import-linter configuration in ``pyproject.toml``, which
checks the imports it can see. What it cannot see is checked here by
running it: the brains the member's slots name and the bundled commands are
loaded by name or path. The guest and the shared packages are the
``environment-loads-no-host-only-library`` contract's sources, and the
libraries only the host uses its forbidden modules.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import grimp
import yaml

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_CONTRACT = next(
    contract
    for contract in tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    if contract["id"] == "environment-loads-no-host-only-library"
)
#: Every guildbotics package the environment may load: the guest and the
#: shared packages.
_ALLOWED: tuple[str, ...] = tuple(_CONTRACT["source_modules"])
_SHARED = tuple(name for name in _ALLOWED if name != "guildbotics.guest")
#: Third-party libraries only the host uses.
_HOST_ONLY: set[str] = set(_CONTRACT["forbidden_modules"])
_BRAIN_MAPPING = (
    Path(__file__).resolve().parents[2]
    / "guildbotics"
    / "templates"
    / "intelligences"
    / "brain_mapping.yml"
)

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
    importlib.import_module(slot["class"].rpartition(".")[0])
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
    work_kind = "chat"

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
        agent_execution_context={"max_completion_attempts": 2},
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
    name, so no import reaches them)."""
    mapping = yaml.safe_load(_BRAIN_MAPPING.read_text(encoding="utf-8"))
    roots = {"guildbotics.guest.entry", "guildbotics.guest.worktree_copy"} | {
        slot["class"].rpartition(".")[0] for slot in mapping.values()
    }
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
