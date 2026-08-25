from __future__ import annotations

import json
from uuid import uuid4

from guildbotics.hub import connection
from guildbotics.hub.relay_client import HubRelayClient


def test_remote_owner_commands_request_json_output(monkeypatch) -> None:
    workspace_id = "0198ab00-0000-7000-8000-000000000001"
    device_id = str(uuid4())
    calls: list[list[str]] = []

    def run_hub_command(endpoint, arguments):
        calls.append(arguments)
        return json.dumps({"workspace_id": workspace_id, "owner": None})

    monkeypatch.setattr(connection, "run_hub_command", run_hub_command)
    client = HubRelayClient(
        connection.HubLocation(endpoint=connection.HubEndpoint(host="hub.local")),
        workspace_id,
        device_id,
        str(uuid4()),
    )

    assert client.owner_get() is None
    assert calls == [["owner", "get", workspace_id, "--format", "json"]]
