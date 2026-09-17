"""Freshness contract for Hub live snapshots.

A publisher stamps ``observed_at`` from its own clock every
``LIVE_HEARTBEAT_INTERVAL_SECONDS``. Viewers and the Hub compare that stamp
to *their* clocks, so delayed and expired thresholds must include expected
clock skew and delivery jitter. A margin smaller than that skew lets a
healthy publisher flicker into the delayed state between heartbeats.

The Hub (expiry), the publisher runtime (heartbeat), and the Desktop API
(delayed display) all import this module so the three numbers cannot drift
apart. Layers that cannot import one another still share one contract
because this file lives in ``utils``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from guildbotics.utils.timestamps import parse_iso_datetime

LIVE_HEARTBEAT_INTERVAL_SECONDS = 10.0
LIVE_CLOCK_SKEW_SECONDS = 15.0
LIVE_DELIVERY_JITTER_SECONDS = 5.0
LIVE_MISSED_HEARTBEATS_BEFORE_EXPIRE = 3
LIVE_DELAY_SECONDS = (
    LIVE_HEARTBEAT_INTERVAL_SECONDS
    + LIVE_CLOCK_SKEW_SECONDS
    + LIVE_DELIVERY_JITTER_SECONDS
)
LIVE_EXPIRE_AFTER_SECONDS = (
    LIVE_DELAY_SECONDS
    + LIVE_MISSED_HEARTBEATS_BEFORE_EXPIRE * LIVE_HEARTBEAT_INTERVAL_SECONDS
)

LiveFreshness = Literal["online", "delayed", "expired"]


def live_status(observed_at: str, now: datetime | None = None) -> LiveFreshness:
    """Classify a snapshot from the age of ``observed_at`` on this device's clock.

    Args:
        observed_at: Publisher timestamp, usually ISO-8601.
        now: Viewer clock. Production callers omit this; tests pass a frozen
            instant so transitions stay deterministic.

    Returns:
        ``online`` while a healthy publisher can still be in the heartbeat
        gap, ``delayed`` after that, and ``expired`` after the Hub would
        delete the file. Unreadable timestamps are expired.
    """
    timestamp = parse_iso_datetime(observed_at)
    if timestamp is None:
        return "expired"
    age = ((now or datetime.now(UTC)) - timestamp.astimezone(UTC)).total_seconds()
    if age > LIVE_EXPIRE_AFTER_SECONDS:
        return "expired"
    if age > LIVE_DELAY_SECONDS:
        return "delayed"
    return "online"
