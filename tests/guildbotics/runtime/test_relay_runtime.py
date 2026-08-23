from __future__ import annotations

import json
import time
from types import SimpleNamespace
from uuid import uuid4

from guildbotics.runtime.relay_runtime import OWNER_CACHE_SECONDS, RelayRuntime


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
