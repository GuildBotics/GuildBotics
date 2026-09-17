from __future__ import annotations

from datetime import UTC, datetime, timedelta

from guildbotics.hub import relay
from guildbotics.runtime import relay_runtime
from guildbotics.utils.live_freshness import (
    LIVE_CLOCK_SKEW_SECONDS,
    LIVE_DELAY_SECONDS,
    LIVE_DELIVERY_JITTER_SECONDS,
    LIVE_EXPIRE_AFTER_SECONDS,
    LIVE_HEARTBEAT_INTERVAL_SECONDS,
    LIVE_MISSED_HEARTBEATS_BEFORE_EXPIRE,
    live_status,
)

VIEWER_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
OBSERVED_DEVICE_CLOCK_SKEW_SECONDS = 6.0
PREVIOUS_LIVE_DELAY_SECONDS = 15.0


def _observed_at(age_seconds: float) -> str:
    return (VIEWER_NOW - timedelta(seconds=age_seconds)).isoformat()


def test_freshness_thresholds_are_one_derived_contract() -> None:
    assert LIVE_DELAY_SECONDS == (
        LIVE_HEARTBEAT_INTERVAL_SECONDS
        + LIVE_CLOCK_SKEW_SECONDS
        + LIVE_DELIVERY_JITTER_SECONDS
    )
    assert LIVE_EXPIRE_AFTER_SECONDS == (
        LIVE_DELAY_SECONDS
        + LIVE_HEARTBEAT_INTERVAL_SECONDS * LIVE_MISSED_HEARTBEATS_BEFORE_EXPIRE
    )
    assert LIVE_CLOCK_SKEW_SECONDS >= OBSERVED_DEVICE_CLOCK_SKEW_SECONDS
    assert LIVE_DELAY_SECONDS > PREVIOUS_LIVE_DELAY_SECONDS


def test_hub_and_publisher_bind_the_same_contract_objects() -> None:
    assert relay.LIVE_EXPIRE_AFTER_SECONDS is LIVE_EXPIRE_AFTER_SECONDS
    assert (
        relay_runtime.LIVE_HEARTBEAT_INTERVAL_SECONDS is LIVE_HEARTBEAT_INTERVAL_SECONDS
    )


def test_healthy_publisher_stays_online_when_its_clock_lags() -> None:
    for seconds_since_beat in range(int(LIVE_HEARTBEAT_INTERVAL_SECONDS) + 1):
        age = seconds_since_beat + OBSERVED_DEVICE_CLOCK_SKEW_SECONDS
        assert live_status(_observed_at(age), now=VIEWER_NOW) == "online"
    age = LIVE_HEARTBEAT_INTERVAL_SECONDS + OBSERVED_DEVICE_CLOCK_SKEW_SECONDS
    assert age > PREVIOUS_LIVE_DELAY_SECONDS


def test_age_at_the_delay_boundary_is_still_online() -> None:
    assert live_status(_observed_at(LIVE_DELAY_SECONDS), now=VIEWER_NOW) == "online"
    assert (
        live_status(_observed_at(LIVE_DELAY_SECONDS + 0.001), now=VIEWER_NOW)
        == "delayed"
    )


def test_stopped_publisher_becomes_delayed_then_expired() -> None:
    just_delayed = LIVE_DELAY_SECONDS + 0.001
    at_expire = LIVE_EXPIRE_AFTER_SECONDS
    just_expired = LIVE_EXPIRE_AFTER_SECONDS + 0.001
    assert live_status(_observed_at(just_delayed), now=VIEWER_NOW) == "delayed"
    assert live_status(_observed_at(at_expire), now=VIEWER_NOW) == "delayed"
    assert live_status(_observed_at(just_expired), now=VIEWER_NOW) == "expired"


def test_publisher_clock_ahead_of_the_viewer_stays_online() -> None:
    future = (VIEWER_NOW + timedelta(seconds=LIVE_CLOCK_SKEW_SECONDS)).isoformat()
    assert live_status(future, now=VIEWER_NOW) == "online"


def test_unreadable_observed_at_is_expired() -> None:
    assert live_status("not-a-timestamp", now=VIEWER_NOW) == "expired"
