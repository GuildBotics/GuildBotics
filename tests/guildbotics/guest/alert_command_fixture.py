"""Exercise the guest alert command with the Rust member response on stdin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from guildbotics.guest.window import WindowCodeHostingService


async def main() -> None:
    """Keep the guest's real service adapter and display code in the check."""
    member_response = json.load(sys.stdin)
    spec = importlib.util.spec_from_file_location(
        "alert_command",
        Path(__file__).parents[3]
        / "guildbotics/templates/commands/repository/security_alerts.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Client:
        async def acall(self, name: str, **params):
            """Use the exact read returned by the Rust member dispatcher."""
            assert name == "member"
            arguments = params["arguments"]
            assert arguments[:2] == ["repo-info", "read"]
            assert arguments[arguments.index("--resource") + 1] == "dependency_alerts"
            assert arguments[arguments.index("--subject") + 1] == "acme/demo"
            return {"exit_code": 0, "stdout": json.dumps(member_response), "stderr": ""}

    code = WindowCodeHostingService(Client(), "aiko")
    shown = await module.main(
        SimpleNamespace(
            person=SimpleNamespace(person_id="aiko"),
            get_code_hosting_service=lambda: code,
        ),
        "acme/demo",
        output="json",
    )
    print(shown)


if __name__ == "__main__":
    asyncio.run(main())
