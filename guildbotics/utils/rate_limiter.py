import asyncio
import os
import threading
import time
from typing import List

REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    import redis.asyncio as redis

    _redis_client = redis.from_url(REDIS_URL)
else:
    _redis_client = None


class RateLimiter:
    """Utility for rate limiting using RateLimit settings.

    Attributes:
        name (str): Identifier for the limiter.
        max_requests_per_minute (int): Max requests allowed per minute.
        _lock (threading.Lock): Lock for thread-safe updates.
        _use_redis (bool): Whether to use Redis for rate limiting.
    """

    def __init__(self, name: str, max_requests_per_minute: int):
        """Initializes the RateLimiter.

        Args:
            name (str): Identifier for this limiter.
            max_requests_per_minute (int): Max requests allowed per minute.
        """
        self.name = name
        self.max_requests_per_minute = max_requests_per_minute
        self._lock: threading.Lock = threading.Lock()
        self._request_timestamps: List[float] = []
        self._use_redis = _redis_client is not None
        if self._use_redis:
            self._redis = _redis_client
            self._redis_key = f"rate_limiter:{self.name}"

    async def acquire(self) -> None:
        """Waits if necessary to comply with the rate limit."""
        window = 60
        if self._use_redis:
            # Redis-based sliding window implementation
            while True:
                now = time.time()
                # Remove old entries
                await self._redis.zremrangebyscore(
                    self._redis_key, "-inf", now - window
                )
                count = await self._redis.zcard(self._redis_key)
                if count < self.max_requests_per_minute:
                    await self._redis.zadd(self._redis_key, {now: now})
                    await self._redis.expire(self._redis_key, window)
                    return
                # Next available slot
                oldest = await self._redis.zrange(
                    self._redis_key, 0, 0, withscores=True
                )
                oldest_ts = oldest[0][1] if oldest else now
                sleep_time = max(0.1, window - (now - oldest_ts))
                await asyncio.sleep(sleep_time)
        else:
            # In-memory implementation
            while True:
                async with AsyncLock(self._lock):
                    now = time.time()
                    # Remove old timestamps for per-minute limit
                    self._request_timestamps = [
                        ts for ts in self._request_timestamps if now - ts < window
                    ]

                    if len(self._request_timestamps) < self.max_requests_per_minute:
                        self._request_timestamps.append(now)
                        return

                    # Calculate sleep time based on when the oldest request will expire
                    oldest = min(self._request_timestamps)
                    sleep_time = max(0.1, window - (now - oldest))

                # Sleep outside the lock to allow other tasks to proceed
                await asyncio.sleep(sleep_time)


class AsyncLock:
    """Async context manager for threading.Lock.

    Args:
        lock (threading.Lock): Standard threading lock.
    """

    def __init__(self, lock: threading.Lock):
        self._lock: threading.Lock = lock

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._lock.acquire)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


_limiters_lock = threading.Lock()
_limiters: dict[str, RateLimiter] = {}


async def acquire(name: str, max_requests_per_minute: int) -> None:
    """Acquire a rate limiter for the given name.

    A separate RateLimiter instance is created for each unique `name`
    and reused on subsequent calls.

    Args:
        name (str): Identifier for the limiter.
        max_requests_per_minute (int): Maximum requests allowed per minute.
    """

    # Ensure only one RateLimiter instance per name
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _limiters_lock.acquire)
    try:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(name, max_requests_per_minute)
            _limiters[name] = limiter
    finally:
        _limiters_lock.release()

    # Wait until the limiter allows the next request
    await limiter.acquire()
