"""The command execution machinery imports nothing only the host may hold.

The machinery (``guildbotics.commands.*``, the runner included) is what runs a
command and its subcommands; the host entries that start a run open the
isolated environment around it. So importing the machinery must load neither
the environment, the member broker, the scheduler and dispatchers, nor the
third-party libraries they bring (a server stack, the microVM SDK, the OS
keychain).
"""

from __future__ import annotations

import json
import subprocess
import sys

#: Every guildbotics module the machinery may load, by package or module. An
#: allowlist, so a new import path into a host-only module fails here instead
#: of waiting to be added to a list of forbidden ones.
_ALLOWED = (
    "guildbotics.commands",
    "guildbotics.entities",
    "guildbotics.integrations.chat_service",
    "guildbotics.integrations.ticket_manager",
    "guildbotics.intelligences.brains.brain",
    "guildbotics.intelligences.common",
    "guildbotics.intelligences.functions",
    "guildbotics.loader",
    "guildbotics.runtime",
    "guildbotics.utils",
)
#: The packages whose ``__init__`` alone the allowed modules above load.
_PARENTS = {
    "guildbotics",
    "guildbotics.integrations",
    "guildbotics.intelligences",
    "guildbotics.intelligences.brains",
}
#: Third-party libraries only the host uses.
_HOST_ONLY = {
    "cryptography",
    "fastapi",
    "httpx",
    "jwt",
    "keyring",
    "mcp",
    "microsandbox",
    "opentelemetry",
    "rich",
    "slack_sdk",
    "sse_starlette",
    "starlette",
    "uvicorn",
    "watchfiles",
}

_PROBE = """
import importlib, json, pkgutil, sys
import guildbotics.commands as package
for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
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
    def require_completion(self, run_id):
        calls.append("require_completion")
        if calls.count("require_completion") == 1:
            raise RuntimeError("not completed")

    def evidence(self, run_id):
        calls.append("evidence")
        return []

    def record_completed(self, run_id, attempt):
        calls.append("record_completed")

    def record_completion_missing(self, run_id, attempt, max_attempts, error):
        calls.append("record_completion_missing")

async def invoke_once(name, args, kwargs, cwd):
    return "response"

runner = CommandRunner.__new__(CommandRunner)
runner._ledger = Ledger()
runner._invoke_once = invoke_once
result = asyncio.run(
    runner._invoke(
        "functions/handle_chat_event",
        agent_execution_context={
            "run_id": "run-1",
            "work_kind": "chat",
            "max_completion_attempts": 2,
        },
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
        if name.startswith("guildbotics")
        and name not in _PARENTS
        and not any(
            name == allowed or name.startswith(allowed + ".") for allowed in _ALLOWED
        )
    ] + sorted({name.split(".")[0] for name in loaded} & _HOST_ONLY)


def test_importing_the_machinery_loads_nothing_host_only() -> None:
    loaded = _run(_PROBE)

    assert isinstance(loaded, list)
    assert "guildbotics.commands.runner" in loaded
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
