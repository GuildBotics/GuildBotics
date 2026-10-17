from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from guildbotics.drivers.relay_runtime import OWNER_CACHE_SECONDS, RelayRuntime
from guildbotics.drivers.service_owner import create_relay_runtime
from guildbotics.hub.connection import HubEndpoint, HubLocation, hub_remote_url
from tests.guildbotics.integrations.branvil_double import connect_admin


class _Client:
    workspace_id = "0198ab00-0000-7000-8000-000000000001"
    device_id = str(uuid4())
    publisher_id = str(uuid4())

    def publish_line(self, line: str) -> None:
        return None


class _OwnerClient(_Client):
    def __init__(self, owner_device_id: str | None) -> None:
        self.owner_device_id = owner_device_id
        self.owner_calls = 0

    def owner_get(self):
        self.owner_calls += 1
        if self.owner_device_id is None:
            raise OSError("Hub unavailable")
        return SimpleNamespace(owner_device_id=self.owner_device_id)


def test_owner_probe_is_cached_but_hub_failures_are_not() -> None:
    client = _OwnerClient(_Client.device_id)
    runtime = RelayRuntime(client)

    assert runtime.check_owner() is True
    assert runtime.check_owner() is True
    assert client.owner_calls == 1

    client.owner_device_id = None
    runtime._owner_cache = (
        time.monotonic() - OWNER_CACHE_SECONDS - 1,
        True,
    )
    assert runtime.check_owner() is None
    assert runtime.check_owner() is None
    assert client.owner_calls == 3


def test_expired_live_event_removes_work_without_losing_offline_state() -> None:
    expired: list[tuple[str, str, str]] = []
    runtime = RelayRuntime(
        _Client(), on_live_expired=lambda *value: expired.append(value)
    )

    runtime._handle_line(
        json.dumps(
            {
                "kind": "live-expired",
                "schema_version": 1,
                "workspace_id": _Client.workspace_id,
                "device_id": _Client.device_id,
                "publisher_id": _Client.publisher_id,
                "observed_at": "2026-08-23T00:00:00+00:00",
            }
        )
    )

    assert expired == [
        (_Client.device_id, _Client.publisher_id, "2026-08-23T00:00:00+00:00")
    ]


def test_new_live_schema_is_reported_as_a_client_update() -> None:
    errors: list[str] = []
    runtime = RelayRuntime(_Client(), on_relay_error=errors.append)

    runtime._handle_line(
        json.dumps(
            {
                "schema_version": 999,
                "workspace_id": _Client.workspace_id,
                "device_id": _Client.device_id,
                "publisher_id": _Client.publisher_id,
                "observed_at": "2026-08-23T00:00:00+00:00",
                "works": [],
            }
        )
    )

    assert errors == ["live_client_update_required"]


def _project(root: Path, services: str) -> Path:
    project = root / ".guildbotics/config/team/project.yml"
    project.parent.mkdir(parents=True)
    project.write_text(f"language: en\nservices:\n{services}")
    return root


def test_another_machine_forwards_the_hub_ports_its_saved_services_reach(
    tmp_path: Path,
) -> None:
    workspace_id = "0198ab00-0000-7000-8000-000000000001"
    root = _project(
        tmp_path, "  code_hosting_service:\n    name: branvil\n    workspace: a\n"
    )
    connect_admin(base_url="http://127.0.0.1:17900")
    remote = hub_remote_url(HubLocation(HubEndpoint("hub")), workspace_id)

    runtime = create_relay_runtime(
        remote, workspace_id, str(uuid4()), workspace_root=root
    )

    assert runtime._forwards is not None
    assert runtime._forwards._endpoint == HubEndpoint("hub")
    assert runtime._forwards._ports() == {17900}
    # A change of the saved services is read on the next round.
    (root / ".guildbotics/config/team/project.yml").write_text(
        "language: en\nservices: {}\n"
    )
    assert runtime._forwards._ports() == set()


def test_the_hub_machine_forwards_nothing(tmp_path: Path) -> None:
    workspace_id = "0198ab00-0000-7000-8000-000000000001"
    root = _project(tmp_path, "  ticket_service:\n    name: branvil\n")
    remote = hub_remote_url(HubLocation(), workspace_id)

    runtime = create_relay_runtime(
        remote, workspace_id, str(uuid4()), workspace_root=root
    )

    assert runtime._forwards is None


class _Forwards:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self, timeout: float = 5.0) -> bool:
        self.events.append("stop")
        return True


class _WatchClient(_Client):
    def watch(self, on_line, stop) -> None:
        stop.wait()

    def close(self) -> None:
        return None


def test_the_forwards_run_as_long_as_the_relay() -> None:
    forwards = _Forwards()
    runtime = RelayRuntime(_WatchClient(), forwards=forwards)

    runtime.start()
    assert forwards.events == ["start"]
    assert runtime.stop() is True
    assert forwards.events == ["start", "stop"]
