import asyncio
import os
import threading
import time

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
        self._request_timestamps: list[float] = []
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
                # No await in this short section: acquisition and release stay
                # together even when the calling coroutine is cancelled.
                with self._lock:
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

    # Keep the no-await lookup atomic across threads and event loops, without
    # handing lock ownership between a worker and a cancellable coroutine.
    with _limiters_lock:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(name, max_requests_per_minute)
            _limiters[name] = limiter
    # Wait until the limiter allows the next request
    await limiter.acquire()
