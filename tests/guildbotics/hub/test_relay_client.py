from __future__ import annotations

import json
from uuid import uuid4

from guildbotics.hub import connection
from guildbotics.hub import relay_client as relay_client_module
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


class _DeadProcess:
    """A watch process that connected to nothing and exited at once."""

    stdout: list[str] = []

    def poll(self) -> int:
        return 0


class _RecordingStop:
    """A stop event that records each wait and stops after enough of them."""

    def __init__(self, stop_after: int) -> None:
        self.waits: list[float] = []
        self._stop_after = stop_after

    def is_set(self) -> bool:
        return len(self.waits) >= self._stop_after

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        return self.is_set()


def _remote_client() -> HubRelayClient:
    return HubRelayClient(
        connection.HubLocation(endpoint=connection.HubEndpoint(host="hub.local")),
        "0198ab00-0000-7000-8000-000000000001",
        str(uuid4()),
        str(uuid4()),
    )


def test_watch_backs_off_while_connections_keep_dying_young(monkeypatch) -> None:
    """One SSH spawn per second against a hub that is down floods the name
    resolver -- observed on Windows to break .local resolution machine-wide --
    so attempts that die young are spaced out, up to a cap."""
    client = _remote_client()
    monkeypatch.setattr(client, "_open_watch_process", lambda: _DeadProcess())
    stop = _RecordingStop(stop_after=6)

    client.watch(lambda line: None, stop, reconnect_delay=1.0, max_reconnect_delay=8.0)

    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_watch_resets_the_backoff_once_a_connection_lives(monkeypatch) -> None:
    client = _remote_client()
    monkeypatch.setattr(client, "_open_watch_process", lambda: _DeadProcess())
    # One clock reading per attempt boundary: the first connection lives for
    # 100 simulated seconds, the second dies at once.
    readings = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(
        relay_client_module.time, "monotonic", lambda: next(readings, 100.0)
    )
    stop = _RecordingStop(stop_after=2)

    client.watch(lambda line: None, stop, reconnect_delay=1.0, max_reconnect_delay=8.0)

    assert stop.waits == [1.0, 1.0]
