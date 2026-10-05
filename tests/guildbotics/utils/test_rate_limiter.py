import asyncio
import importlib
import sys
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_site", ["registry", "timestamps"])
async def test_cancellation_at_lock_acquisition_releases_lock(monkeypatch, lock_site):
    """Cancellation at the worker/coroutine handoff must not orphan either lock."""
    module = _import_inmemory_rate_limiter(monkeypatch)
    loop = asyncio.get_running_loop()
    acquired = threading.Lock()
    task = None

    class CancelOnAcquire:
        def acquire(self):
            acquired.acquire()
            loop.call_soon_threadsafe(task.cancel)
            return True

        def release(self):
            acquired.release()

        def __enter__(self):
            self.acquire()

        def __exit__(self, *args):
            self.release()

    limiter = module.RateLimiter("cancel", 10)
    module._limiters["cancel"] = limiter
    owner, attribute = (
        (module, "_limiters_lock") if lock_site == "registry" else (limiter, "_lock")
    )
    monkeypatch.setattr(owner, attribute, CancelOnAcquire())

    async def request():
        await module.acquire("cancel", 10)
        # Synchronous acquisition also delivers cancellation at the next await.
        await asyncio.sleep(0)

    task = asyncio.create_task(request())
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not acquired.locked()
    finally:
        # A regression must fail without stranding the test executor's workers.
        if acquired.locked():
            acquired.release()

    monkeypatch.setattr(owner, attribute, acquired)
    await asyncio.wait_for(module.acquire("cancel", 10), timeout=1)


@pytest.mark.asyncio
async def test_cancellation_during_rate_limit_sleep_preserves_next_request(monkeypatch):
    module = _import_inmemory_rate_limiter(monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(_time, "time", clock.time)
    sleeping = asyncio.Event()

    async def sleep(seconds):
        assert seconds == 60
        assert not module._limiters_lock.locked()
        assert not module._limiters["limited"]._lock.locked()
        sleeping.set()
        await asyncio.Future()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await module.acquire("limited", 1)
    task = asyncio.create_task(module.acquire("limited", 1))
    await asyncio.wait_for(sleeping.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(module.acquire("other", 1), timeout=1)
    clock.now += 60
    await asyncio.wait_for(module.acquire("limited", 1), timeout=1)
    assert module._limiters["limited"]._request_timestamps == [clock.now]


def test_limiter_is_shared_across_threads_and_event_loops(monkeypatch):
    module = _import_inmemory_rate_limiter(monkeypatch)
    barrier = threading.Barrier(4)

    def request():
        barrier.wait(timeout=5)

        async def acquire_many():
            for _ in range(10):
                await module.acquire("shared", 100)
            return module._limiters["shared"]

        return asyncio.run(acquire_many())

    with ThreadPoolExecutor(max_workers=4) as executor:
        limiters = list(executor.map(lambda _: request(), range(4)))
    assert all(limiter is limiters[0] for limiter in limiters)
    assert len(limiters[0]._request_timestamps) == 40
