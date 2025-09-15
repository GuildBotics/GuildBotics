import asyncio
import importlib
import sys
import time as _time
from pathlib import Path

import pytest


def _import_inmemory_rate_limiter(monkeypatch):
    """Import the rate_limiter module ensuring in-memory mode.

    This removes `REDIS_URL` from the environment and reloads the module so
    that the module-level initialization path selects the in-memory backend.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Module: The imported `guildbotics.utils.rate_limiter` module.
    """
    # Ensure REDIS_URL is unset so in-memory implementation is used on import
    monkeypatch.delenv("REDIS_URL", raising=False)

    # Reload the module cleanly to re-evaluate top-level env checks
    if "guildbotics.utils.rate_limiter" in sys.modules:
        del sys.modules["guildbotics.utils.rate_limiter"]

    module = importlib.import_module("guildbotics.utils.rate_limiter")
    importlib.reload(module)

    # Sanity check: in-memory mode must be active
    assert getattr(module, "_redis_client", None) is None
    return module


class FakeClock:
    """A simple fake clock to control time and sleeping.

    The rate limiter uses `time.time()` to determine the sliding window and
    `asyncio.sleep()` to wait. We monkeypatch both to advance virtual time
    deterministically without real delays.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        """Initialize the fake clock.

        Args:
            start: Initial epoch seconds for the clock.
        """
        self.now = start

    def time(self) -> float:
        """Return current virtual time in seconds."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time by the given seconds without real waiting."""
        if seconds and seconds > 0:
            self.now += seconds
        # No real sleep; return control to the event loop immediately
        return None


@pytest.mark.asyncio
async def test_rate_limiter_inmemory_one_minute_window(monkeypatch):
    """Verify in-memory limiter enforces a 1-minute sliding window.

    - REDIS_URL is unset to force in-memory backend.
    - Launch 4 concurrent acquires with limit=3 using asyncio.gather.
    - First 3 proceed immediately; the 4th waits until the 1-minute window rolls.
    - Fake time advances exactly 60s due to the enforced wait.
    """

    # Ensure repository root is importable (for direct pytest invocation)
    repo_root = str(Path(__file__).resolve().parents[3])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    rate_limiter = _import_inmemory_rate_limiter(monkeypatch)

    # Install fake clock for deterministic time and sleep behavior
    clock = FakeClock(start=10_000.0)
    monkeypatch.setattr(_time, "time", clock.time)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)

    async def acquire_once(idx: int):
        # Use a fixed name to share the same limiter instance
        await rate_limiter.acquire("test-window", max_requests_per_minute=3)

    # Start 4 concurrent acquires; with limit=3, the 4th must wait ~60s
    await asyncio.gather(*(acquire_once(i) for i in range(4)))

    # The fake time should have advanced by exactly 60 seconds
    assert clock.now == pytest.approx(10_060.0)
