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


def test_importing_the_machinery_loads_nothing_host_only() -> None:
    loaded = json.loads(
        subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )

    assert "guildbotics.commands.runner" in loaded
    assert [
        name
        for name in loaded
        if name.startswith("guildbotics")
        and name not in _PARENTS
        and not any(
            name == allowed or name.startswith(allowed + ".") for allowed in _ALLOWED
        )
    ] == []
    assert sorted({name.split(".")[0] for name in loaded} & _HOST_ONLY) == []
