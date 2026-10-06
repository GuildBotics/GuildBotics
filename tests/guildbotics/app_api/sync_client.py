"""A request-only API client that owns its synchronization teardown."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.sync import activation, current_sync_manager
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port


@contextmanager
def workspace_sync_client() -> Iterator[TestClient]:
    """Stop the client's queue and relay even when a test makes stop refuse.

    These endpoint tests start synchronization themselves, without the app's
    lifespan (diagnostics maintenance and other unrelated services).
    """
    runtime = AppRuntime(EventBus())
    client = TestClient(create_app(session_token="secret", runtime=runtime))
    service = runtime.workspace_sync_service
    try:
        yield client
    finally:
        try:
            if not service.deactivate():
                # Bypass an instance-level stop refusal, then release the slot.
                manager = current_sync_manager()
                if manager is not None:
                    assert GitSyncManager.stop(manager, timeout=10), (
                        "a synchronization worker outlived its test"
                    )
                activation._manager = None
                activation._workspace = None
                set_workspace_sync_port(None)
        finally:
            service._stop_relay()
            client.close()
