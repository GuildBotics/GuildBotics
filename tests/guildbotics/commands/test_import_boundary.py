"""What runs inside a command's isolated environment imports nothing only the
host may hold.

The environment's entry runs the command execution machinery
(``guildbotics.commands.*``), the brains the member's slots name, the AI CLI
adapters, the bundled commands, and the client of the command's window to the
host. None of it may load the environment itself, the member broker, the
credential gateway, the scheduler and dispatchers, the writers of the
diagnostics and the activity (records are the host's to write), nor the
third-party libraries only the host uses (a server stack, the microVM SDK,
the OS keychain, the inference SDK).
"""

from __future__ import annotations

import json
import subprocess
import sys

#: Every guildbotics module the environment may load, by package or module. An
#: allowlist, so a new import path into a host-only module fails here instead
#: of waiting to be added to a list of forbidden ones.
_ALLOWED = (
    "guildbotics.commands",
    "guildbotics.editions.edition",
    "guildbotics.editions.simple.simple_brain_factory",
    "guildbotics.editions.simple.simple_loader_factory",
    "guildbotics.entities",
    "guildbotics.integrations.chat_service",
    "guildbotics.integrations.code_hosting_service",
    "guildbotics.integrations.ticket_manager",
    "guildbotics.integrations.window",
    "guildbotics.intelligences.agent_runtime.acp",
    "guildbotics.intelligences.agent_runtime.antigravity",
    "guildbotics.intelligences.agent_runtime.claude",
    "guildbotics.intelligences.agent_runtime.codex",
    "guildbotics.intelligences.agent_runtime.copilot",
    "guildbotics.intelligences.agent_runtime.factory",
    "guildbotics.intelligences.agent_runtime.grok",
    "guildbotics.intelligences.agent_runtime.host_client",
    "guildbotics.intelligences.agent_runtime.jsonrpc",
    "guildbotics.intelligences.agent_runtime.models",
    "guildbotics.intelligences.agent_runtime.provider_process",
    "guildbotics.intelligences.agent_runtime.turn",
    "guildbotics.intelligences.agent_runtime.usage_snapshots",
    "guildbotics.intelligences.brains.agno_agent",
    "guildbotics.intelligences.brains.brain",
    "guildbotics.intelligences.brains.cli_agent",
    "guildbotics.intelligences.brains.inference",
    "guildbotics.intelligences.brains.jev",
    "guildbotics.intelligences.brains.util",
    "guildbotics.intelligences.cli_agents",
    "guildbotics.intelligences.common",
    "guildbotics.intelligences.effort",
    "guildbotics.intelligences.functions",
    "guildbotics.loader",
    "guildbotics.runtime",
    "guildbotics.utils",
)
#: The packages whose ``__init__`` alone the allowed modules above load:
#: ``guildbotics.observability``'s is the correlation of spans, which a brain
#: sends the host with what it records; its writers stay out.
_PARENTS = {
    "guildbotics",
    "guildbotics.editions",
    "guildbotics.editions.simple",
    "guildbotics.integrations",
    "guildbotics.intelligences",
    "guildbotics.intelligences.agent_runtime",
    "guildbotics.intelligences.brains",
    "guildbotics.observability",
}
#: Third-party libraries only the host uses. ``httpx`` is not one: the window
#: to the host is spoken with it (and it brings ``rich`` for its own CLI).
_HOST_ONLY = {
    "agno",
    "cryptography",
    "fastapi",
    "jwt",
    "keyring",
    "mcp",
    "microsandbox",
    "opentelemetry",
    "slack_sdk",
    "sse_starlette",
    "starlette",
    "uvicorn",
    "watchfiles",
}

#: Loads the environment's entry, every brain the bundled mapping names, every
#: adapter, every module of the machinery, and every bundled Python command
#: the way the machinery loads one.
_PROBE = """
import importlib, json, pkgutil, sys
from pathlib import Path
import guildbotics.commands as package
from guildbotics.commands.python_command import _load_python_module
import guildbotics.runtime.command_entry
import guildbotics.intelligences.agent_runtime.factory
from guildbotics.utils.fileio import get_template_path, load_yaml_file
for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
templates = get_template_path()
mapping = load_yaml_file(templates / "intelligences" / "brain_mapping.yml")
for slot in mapping.values():
    importlib.import_module(slot["class"].rpartition(".")[0])
for path in sorted((templates / "commands").rglob("*.py")):
    _load_python_module(path)
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
        and name not in _PARENTS
        and not any(
            name == allowed or name.startswith(allowed + ".") for allowed in _ALLOWED
        )
    ] + sorted({name.split(".")[0] for name in loaded} & _HOST_ONLY)


def test_what_runs_in_the_environment_loads_nothing_host_only() -> None:
    loaded = _run(_PROBE)

    assert isinstance(loaded, list)
    assert {
        "guildbotics.commands.runner",
        "guildbotics.intelligences.brains.cli_agent",
        "guildbotics.intelligences.agent_runtime.codex",
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
