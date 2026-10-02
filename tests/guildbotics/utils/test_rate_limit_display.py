import pytest

from guildbotics.utils.rate_limit_display import rate_limit_reset_display


@pytest.mark.parametrize(
    "local_rate_limit_timezone, at, expected",
    [
        ("Asia/Tokyo", "2026-10-03T06:30:12Z", "2026-10-03 15:30:12+09:00"),
        ("Asia/Tokyo", "2026-10-03T08:30:12+02:00", "2026-10-03 15:30:12+09:00"),
        ("Asia/Kathmandu", "2026-10-03T23:30:12Z", "2026-10-04 05:15:12+05:45"),
        ("America/New_York", "2026-01-03T06:30:12Z", "2026-01-03 01:30:12-05:00"),
        ("America/New_York", "2026-07-03T06:30:12Z", "2026-07-03 02:30:12-04:00"),
    ],
    indirect=["local_rate_limit_timezone"],
)
def test_reset_uses_the_host_zone_at_the_reset_instant(
    local_rate_limit_timezone, at, expected
):
    assert rate_limit_reset_display(at, "Resets in 1h") == ("_with_reset", expected)


@pytest.mark.parametrize("at", ["", "invalid", "2026-10-03T06:30:12", "2026-10-03"])
@pytest.mark.parametrize("hint", ["", "Resets in 1h"])
def test_unknown_or_naive_reset_never_invents_a_zone(at, hint):
    assert rate_limit_reset_display(at, hint) == ("_with_hint" if hint else "", hint)
