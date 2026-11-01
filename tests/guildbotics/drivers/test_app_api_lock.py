from __future__ import annotations

import os
import subprocess
import sys

import pytest

from guildbotics.drivers.app_api_lock import (
    BackendAlreadyRunningError,
    BackendMetadata,
    hold_backend_lock,
    inspect_backend_lock,
    read_backend_metadata,
)


@pytest.mark.windows
def test_backend_lock_excludes_other_processes_and_names_its_holder():
    with hold_backend_lock(8765):
        assert inspect_backend_lock() == (True, BackendMetadata(os.getpid(), 8765))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from guildbotics.drivers.app_api_lock import hold_backend_lock; "
                    "\nwith hold_backend_lock(9999): raise AssertionError('second backend ran')"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert "BackendAlreadyRunningError" in result.stderr
    assert inspect_backend_lock() == (False, None)
    # The record outlives its holder, which is why callers match the lock.
    assert read_backend_metadata() == BackendMetadata(os.getpid(), 8765)


def test_backend_lock_covers_failures():
    with pytest.raises(ValueError):
        with hold_backend_lock(8765):
            raise ValueError("bind failed")
    assert inspect_backend_lock() == (False, None)


def test_a_second_holder_in_the_same_process_is_refused():
    with hold_backend_lock(8765):
        with pytest.raises(BackendAlreadyRunningError):
            with hold_backend_lock(8765):
                pytest.fail("two backends")
        assert read_backend_metadata() == BackendMetadata(os.getpid(), 8765)
