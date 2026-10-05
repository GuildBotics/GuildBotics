"""Request-only API tests must release the service before HOME changes."""

import threading
from pathlib import Path

import pytest

from guildbotics.hub.connection import HubLocation
from guildbotics.hub.relay import live_root
from guildbotics.hub.relay_client import HubRelayClient
from guildbotics.runtime.relay_runtime import RelayRuntime
from guildbotics.sync import activation, current_sync_manager
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils.workspace_sync_port import (
    NoOpWorkspaceSyncPort,
    get_workspace_sync_port,
)
from tests.guildbotics.app_api.sync_client import workspace_sync_client
from tests.guildbotics.sync.fake_activation import install_memory_activation

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"
DEVICE_ID = "0198ab00-0000-7000-8000-000000000002"
PUBLISHER_ID = "0198ab00-0000-7000-8000-000000000003"


@pytest.mark.parametrize("refuse_stop", [False, True])
def test_teardown_stops_real_workers_before_the_next_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse_stop: bool
) -> None:
    install_memory_activation(monkeypatch)
    heartbeat = threading.Event()
    client = HubRelayClient(HubLocation(), WORKSPACE_ID, DEVICE_ID, PUBLISHER_ID)
    publish = client.publish_line

    def observed_publish(line: str) -> None:
        publish(line)
        heartbeat.set()

    monkeypatch.setattr(client, "publish_line", observed_publish)
    relay = RelayRuntime(client, heartbeat_interval=0.01)
    manager = None
    try:
        with workspace_sync_client() as browser:
            manager = activation.activate_workspace_sync(tmp_path)
            assert manager is not None
            if refuse_stop:
                monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)
            service = browser.app.state.runtime.workspace_sync_service
            service._relay_runtime = relay
            relay.start()
            threads = [relay._watcher, relay._heartbeat_thread, manager._worker]
            assert all(thread is not None and thread.is_alive() for thread in threads)
            heartbeat.clear()
            assert heartbeat.wait(5), "the real heartbeat did not publish"
            assert (
                live_root(WORKSPACE_ID) / DEVICE_ID / f"{PUBLISHER_ID}.json"
            ).is_file()

        assert all(not thread.is_alive() for thread in threads)
        assert service._relay_runtime is None
        assert current_sync_manager() is None
        assert activation._workspace is None
        assert isinstance(get_workspace_sync_port(), NoOpWorkspaceSyncPort)
        next_home = tmp_path / "next-home"
        next_home.mkdir()
        monkeypatch.setenv("HOME", str(next_home))
        monkeypatch.setenv("USERPROFILE", str(next_home))
        heartbeat.clear()
        # Observe several accelerated heartbeat periods after HOME changes.
        assert not heartbeat.wait(0.05)
        assert not live_root(WORKSPACE_ID).exists()
        assert not list(next_home.rglob("*"))
    finally:
        # Keep a failed regression or mutation isolated from following tests.
        relay.stop()
        if manager is not None:
            assert GitSyncManager.stop(manager, timeout=10)
        activation._manager = None
        activation._workspace = None
        activation.deactivate_workspace_sync()
