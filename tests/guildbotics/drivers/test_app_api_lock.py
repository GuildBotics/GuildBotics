from __future__ import annotations

import os
import subprocess
import sys

import pytest

from guildbotics.drivers.app_api_lock import (
    BackendAlreadyRunningError,
    BackendMetadata,
    CONFLICT_NOTICE_PREFIX,
    OWNER_ENV,
    hold_backend_lock,
    inspect_backend_lock,
    read_backend_metadata,
)


@pytest.mark.windows
@pytest.mark.parametrize("owner", ["cli", "desktop"])
def test_backend_lock_excludes_other_processes_and_ignores_stale_records(
    monkeypatch, owner
):
    monkeypatch.setenv(OWNER_ENV, owner)
    with hold_backend_lock() as held:
        assert inspect_backend_lock() == (True, BackendMetadata(owner, os.getpid()))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from guildbotics.drivers.app_api_lock import hold_backend_lock; "
                    "\nwith hold_backend_lock(): raise AssertionError('second backend ran')"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert result.stdout.strip() == CONFLICT_NOTICE_PREFIX + owner
        assert held.metadata.stop_requested is False
        held.request_stop()
        assert read_backend_metadata().stop_requested is True
    assert inspect_backend_lock() == (False, None)
    assert read_backend_metadata().stop_requested is True
    with hold_backend_lock() as restarted:
        assert restarted.metadata.stop_requested is False


def test_backend_lock_covers_failures_and_rejects_unknown_owners(monkeypatch):
    with pytest.raises(ValueError):
        with hold_backend_lock():
            raise ValueError("bind failed")
    assert inspect_backend_lock() == (False, None)
    monkeypatch.setenv(OWNER_ENV, "unknown")
    with pytest.raises(ValueError):
        with hold_backend_lock():
            pytest.fail("invalid owner admitted")


def test_only_the_holder_can_update_its_stop_outcome(monkeypatch):
    with hold_backend_lock() as held:
        held.request_stop()
        with pytest.raises(BackendAlreadyRunningError):
            with hold_backend_lock():
                pytest.fail("two backends")
        assert read_backend_metadata() == held.metadata
