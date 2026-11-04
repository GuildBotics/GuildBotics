from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import json
import pytest
from guildbotics.intelligences.agent_runtime import usage_snapshots
from guildbotics.intelligences.agent_runtime.usage_snapshots import (
    parse_antigravity_usage,
    parse_claude_usage,
    parse_codex_rate_limits,
    parse_copilot_quota,
    parse_grok_billing,
)

_CLAUDE_NOW = datetime(2026, 8, 7, 22, 0, 0, tzinfo=UTC)

_CLAUDE_USAGE_TEXT = """\
You are currently using your subscription to power your Claude Code usage

Current session: 24% used · resets Aug 8, 11:10am (Asia/Tokyo)
Current week (all models): 56% used · resets Aug 8, 10am (Asia/Tokyo)
Current week (Fable): 59% used · resets Aug 8, 10am (Asia/Tokyo)
Extra budget: 5% used

What's contributing to your limits usage?
Last 24h · 313 requests · 7 sessions
  51% of your usage was at >150k context
"""

_GROK_BILLING = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-07T07:37:18.756767+00:00",
            "end": "2026-08-14T07:37:18.756767+00:00",
        },
        "creditUsagePercent": 37.5,
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "prepaidBalance": {"val": 0},
        "isUnifiedBillingUser": True,
        "billingPeriodStart": "2026-08-07T07:37:18.756767+00:00",
        "billingPeriodEnd": "2026-08-14T07:37:18.756767+00:00",
    },
    "subscription_tier": "SuperGrok Lite",
}

_FIXTURES = Path(__file__).parent / "fixtures"
_ANTIGRAVITY_USAGE_FIXTURE = json.loads(
    (_FIXTURES / "antigravity_usage_1_2_5.json").read_text(encoding="utf-8")
)
#: Measured on GitHub Copilot CLI 1.0.86 (``account.getQuota``), anonymized.
_COPILOT_QUOTA_FIXTURE = json.loads(
    (_FIXTURES / "copilot_quota_1_0_86.json").read_text(encoding="utf-8")
)
_GROK_ZERO_USAGE_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "grok_billing_unified_weekly_zero_1_0_34.json"
    ).read_text(encoding="utf-8")
)


def test_parse_codex_rate_limits_reads_camel_case_buckets() -> None:
    # The duration field name has changed across codex versions; one window
    # uses the current name and the other a legacy alias.
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 42.5,
                        "resetsAt": 2_000_000_000,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 78,
                        "resetsAt": 2_000_500_000,
                        "windowMinutes": 10_080,
                    },
                }
            }
        }
    )

    assert snapshot.agent == "codex"
    assert not snapshot.limit_reached
    assert [window.window for window in snapshot.windows] == ["primary", "secondary"]
    primary = snapshot.windows[0]
    assert primary.used_percent == 42.5
    assert primary.resets_at == "2033-05-18T03:33:20+00:00"
    assert primary.window_minutes == 300
    assert primary.label == ""
    assert snapshot.windows[1].window_minutes == 10_080
    assert snapshot.windows[1].label == ""
    assert snapshot.checked_at


def test_parse_codex_rate_limits_reads_snake_case_flat_shape() -> None:
    snapshot = parse_codex_rate_limits(
        {"rate_limits": {"primary": {"used_percent": 100, "resets_at": 2_000_000_000}}}
    )

    assert snapshot.limit_reached
    assert snapshot.windows[0].used_percent == 100
    assert snapshot.windows[0].window_minutes is None


def test_parse_codex_rate_limits_labels_extra_buckets() -> None:
    # Codex App Server reports a separate weekly quota (gpt-reserve) alongside
    # the main 5h/1w windows. Same duration is not a duplicate; the extra
    # bucket's limitName must survive so the UI can tell the two 1w meters
    # apart. Extra buckets may arrive first in the payload; the main windows
    # still lead the snapshot so the compact meters stay 5h, 1w, then extras.
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "base_model_inference": {
                    "limitId": "base_model_inference",
                    "limitName": "gpt-reserve",
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_788_427_524,
                    },
                    "secondary": None,
                },
                "codex": {
                    "limitId": "codex",
                    "limitName": None,
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 300,
                        "resetsAt": 1_787_840_724,
                    },
                    "secondary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_788_427_524,
                    },
                },
            }
        }
    )

    assert [
        (window.window, window.window_minutes, window.label)
        for window in snapshot.windows
    ] == [
        ("primary", 300, ""),
        ("secondary", 10_080, ""),
        ("primary", 10_080, "gpt-reserve"),
    ]


def test_parse_codex_rate_limits_uses_limit_id_when_name_missing() -> None:
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 10, "windowDurationMins": 300},
                },
                "base_model_inference": {
                    "limitName": None,
                    "primary": {"usedPercent": 20, "windowDurationMins": 10_080},
                },
            }
        }
    )

    assert [window.label for window in snapshot.windows] == [
        "",
        "base_model_inference",
    ]


def test_parse_codex_rate_limits_honors_reached_type_flag() -> None:
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "rateLimitReachedType": "primary",
                    "primary": {"usedPercent": 55},
                }
            }
        }
    )

    assert snapshot.limit_reached


def test_parse_codex_rate_limits_drops_unparseable_reset_values() -> None:
    # Anything that is neither epoch seconds nor an ISO timestamp must not
    # leak to the UI as a bogus date string.
    bad = parse_codex_rate_limits(
        {"rate_limits": {"primary": {"used_percent": 10, "resets_at": "bad"}}}
    )
    assert bad.windows[0].resets_at == ""

    iso = parse_codex_rate_limits(
        {
            "rate_limits": {
                "primary": {
                    "used_percent": 10,
                    "resets_at": "2026-07-18T05:00:00+00:00",
                }
            }
        }
    )
    assert iso.windows[0].resets_at == "2026-07-18T05:00:00+00:00"


def test_parse_codex_rate_limits_tolerates_empty_and_malformed_input() -> None:
    for raw in ({}, {"rate_limits": {}}, {"rateLimitsByLimitId": {"x": "bad"}}, None):
        snapshot = parse_codex_rate_limits(raw)
        assert snapshot.windows == []
        assert not snapshot.limit_reached


def test_parse_grok_billing_reads_subscription_window() -> None:
    snapshot = parse_grok_billing(
        _GROK_BILLING, {"authenticated": True, "meta": {"gate": None}}
    )

    assert snapshot.agent == "grok"
    assert not snapshot.limit_reached
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.window == "subscription"
    assert window.used_percent == 37.5
    assert window.resets_at == "2026-08-14T07:37:18.756767+00:00"
    assert window.window_minutes == 10_080
    assert snapshot.checked_at


@pytest.mark.parametrize("gate", [None, "", {}])
def test_parse_grok_billing_normalizes_omitted_unified_weekly_zero(gate: Any) -> None:
    subscription = {
        **_GROK_ZERO_USAGE_FIXTURE["subscription"],
        "meta": {"gate": gate},
    }
    snapshot = parse_grok_billing(
        _GROK_ZERO_USAGE_FIXTURE["billing"],
        subscription,
    )

    assert not snapshot.limit_reached
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.window == "subscription"
    assert window.used_percent == 0
    assert window.resets_at == "2026-09-25T07:37:18.756767+00:00"
    assert window.window_minutes == 10_080


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("subscription", "authenticated", False),
        ("subscription", "authenticated", None),
        ("subscription_meta", "gate", {"reason": "usage_limit"}),
        ("config", "isUnifiedBillingUser", False),
        ("config", "isUnifiedBillingUser", None),
        ("period", "type", "USAGE_PERIOD_TYPE_MONTHLY"),
        ("period", "start", None),
        ("period", "start", "bad"),
        ("period", "end", None),
        ("period", "end", "bad"),
        ("period", "end", "2026-09-18T07:37:18.756767+00:00"),
    ],
)
def test_parse_grok_billing_does_not_infer_zero_without_all_evidence(
    section: str, key: str, value: Any
) -> None:
    billing = json.loads(json.dumps(_GROK_ZERO_USAGE_FIXTURE["billing"]))
    subscription = json.loads(json.dumps(_GROK_ZERO_USAGE_FIXTURE["subscription"]))
    target = {
        "subscription": subscription,
        "subscription_meta": subscription["meta"],
        "config": billing["config"],
        "period": billing["config"]["currentPeriod"],
    }[section]
    if value is None:
        target.pop(key)
    else:
        target[key] = value

    snapshot = parse_grok_billing(billing, subscription)

    assert snapshot.windows == []
    assert snapshot.limit_reached is (section == "subscription_meta")


@pytest.mark.parametrize(
    ("used_percent", "limit_reached"),
    [(0, False), (100, True), (125.5, True)],
)
def test_parse_grok_billing_accepts_subscription_percent_boundaries(
    used_percent: float, limit_reached: bool
) -> None:
    billing = {
        "config": {
            **_GROK_BILLING["config"],
            "creditUsagePercent": used_percent,
        }
    }

    snapshot = parse_grok_billing(billing, {})

    assert snapshot.windows[0].used_percent == used_percent
    assert snapshot.limit_reached is limit_reached


@pytest.mark.parametrize(
    "used_percent", [None, "bad", -1, True, float("nan"), float("inf")]
)
def test_parse_grok_billing_drops_explicit_null_or_invalid_subscription_percent(
    used_percent: Any,
) -> None:
    billing = {
        "config": {
            **_GROK_BILLING["config"],
            "creditUsagePercent": used_percent,
            "onDemandCap": {"val": 100},
            "onDemandUsed": {"val": 50},
        }
    }

    snapshot = parse_grok_billing(billing, _GROK_ZERO_USAGE_FIXTURE["subscription"])

    assert snapshot.windows == []
    assert not snapshot.limit_reached


def test_parse_grok_billing_preserves_gate_without_subscription_percent() -> None:
    snapshot = parse_grok_billing(
        {"config": {"currentPeriod": _GROK_BILLING["config"]["currentPeriod"]}},
        {"authenticated": True, "meta": {"gate": {"reason": "usage_limit"}}},
    )

    assert snapshot.windows == []
    assert snapshot.limit_reached


def test_parse_grok_billing_keeps_percent_without_period() -> None:
    snapshot = parse_grok_billing({"config": {"creditUsagePercent": 12.5}}, {})

    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].used_percent == 12.5
    assert snapshot.windows[0].resets_at == ""
    assert snapshot.windows[0].window_minutes is None


def test_parse_grok_billing_reports_on_demand_credit_percent() -> None:
    billing = {
        "config": {
            **_GROK_BILLING["config"],
            "onDemandCap": {"val": 200},
            "onDemandUsed": {"val": 51},
        }
    }
    snapshot = parse_grok_billing(billing, {})

    on_demand = snapshot.windows[1]
    assert on_demand.window == "on_demand"
    assert on_demand.used_percent == 25.5
    assert not snapshot.limit_reached


def test_parse_grok_billing_marks_limit_on_gate_or_exhausted_credits() -> None:
    gated = parse_grok_billing(
        _GROK_BILLING,
        {"authenticated": True, "meta": {"gate": {"reason": "usage_limit"}}},
    )
    assert gated.limit_reached

    exhausted = parse_grok_billing(
        {
            "config": {
                **_GROK_BILLING["config"],
                "onDemandCap": {"val": 100},
                "onDemandUsed": {"val": 100},
            }
        },
        {},
    )
    assert exhausted.limit_reached


def test_parse_grok_billing_tolerates_empty_and_malformed_input() -> None:
    for billing, subscription in ((None, None), ({}, {}), ({"config": "bad"}, "bad")):
        snapshot = parse_grok_billing(billing, subscription)
        assert snapshot.windows == []
        assert not snapshot.limit_reached


def test_parse_claude_usage_reads_session_and_weekly_windows() -> None:
    snapshot = parse_claude_usage(_CLAUDE_USAGE_TEXT, now=_CLAUDE_NOW)

    assert snapshot.agent == "claude"
    assert not snapshot.limit_reached
    assert [
        (window.window, window.used_percent, window.label)
        for window in snapshot.windows
    ] == [
        ("session", 24.0, ""),
        ("week", 56.0, ""),
        ("current_week_fable", 59.0, "Fable"),
        ("extra_budget", 5.0, "Extra budget"),
    ]
    session, week, fable, extra = snapshot.windows
    assert session.resets_at == "2026-08-08T11:10:00+09:00"
    assert session.window_minutes == 300
    assert week.resets_at == "2026-08-08T10:00:00+09:00"
    assert week.window_minutes == 10_080
    assert fable.resets_at == "2026-08-08T10:00:00+09:00"
    assert fable.window_minutes == 10_080
    # A budget line whose period is unknown reports none rather than a guess.
    assert (extra.resets_at, extra.window_minutes) == ("", None)
    assert snapshot.checked_at


def test_parse_claude_usage_marks_limit_at_full_window() -> None:
    snapshot = parse_claude_usage(
        "Current session: 100% used · resets Aug 8, 11:10am (Asia/Tokyo)",
        now=_CLAUDE_NOW,
    )
    assert snapshot.limit_reached


def test_parse_claude_usage_drops_unusable_reset_times() -> None:
    # A missing timezone or an unknown one makes the instant ambiguous, and a
    # malformed phrase must not survive as a bogus timestamp.
    for reset in ("Aug 8, 11:10am", "Aug 8, 11:10am (Mars/Olympus)", "tomorrow"):
        snapshot = parse_claude_usage(
            f"Current session: 10% used · resets {reset}", now=_CLAUDE_NOW
        )
        assert snapshot.windows[0].resets_at == ""
        assert snapshot.windows[0].used_percent == 10.0


def test_parse_claude_usage_rolls_reset_into_next_year() -> None:
    snapshot = parse_claude_usage(
        "Current week (all models): 12% used · resets Jan 2, 12am (UTC)",
        now=datetime(2026, 12, 30, 12, 0, 0, tzinfo=UTC),
    )
    assert snapshot.windows[0].resets_at == "2027-01-02T00:00:00+00:00"


def test_parse_claude_usage_tolerates_empty_and_malformed_input() -> None:
    for raw in ("", "no usage here", None, {"unexpected": "shape"}):
        snapshot = parse_claude_usage(raw, now=_CLAUDE_NOW)
        assert snapshot.windows == []
        assert not snapshot.limit_reached


def test_parse_antigravity_usage_reads_measured_model_group_windows() -> None:
    snapshot = parse_antigravity_usage(_ANTIGRAVITY_USAGE_FIXTURE)

    assert snapshot.agent == "antigravity"
    assert snapshot.limit_reached
    assert [
        (
            window.window,
            round(window.used_percent, 4),
            window.window_minutes,
            window.label,
            window.resets_at,
        )
        for window in snapshot.windows
    ] == [
        (
            "weekly",
            6.22,
            10_080,
            "Gemini models",
            "2026-09-25T07:50:32+00:00",
        ),
        (
            "5h",
            37.5,
            300,
            "Gemini models",
            "2026-09-18T15:07:00+00:00",
        ),
        (
            "weekly",
            0.0,
            10_080,
            "Claude and GPT models",
            "2026-09-25T07:50:32+00:00",
        ),
        (
            "5h",
            100.0,
            300,
            "Claude and GPT models",
            "2026-09-18T15:07:00+00:00",
        ),
    ]
    assert snapshot.checked_at


@pytest.mark.parametrize("remaining", [0, 0.0, 1, 1.0, 0.5])
def test_parse_antigravity_usage_accepts_remaining_fraction_boundaries(
    remaining: float,
) -> None:
    snapshot = parse_antigravity_usage(
        _antigravity_payload({"remaining_fraction": remaining})
    )

    window = snapshot.windows[0]
    assert window.used_percent == pytest.approx((1.0 - remaining) * 100.0)
    assert snapshot.limit_reached is (remaining == 0)


@pytest.mark.parametrize(
    "remaining",
    [None, "bad", True, False, -0.1, 1.1, float("nan"), float("inf"), float("-inf")],
)
def test_parse_antigravity_usage_drops_unusable_remaining_fraction(
    remaining: Any,
) -> None:
    snapshot = parse_antigravity_usage(
        _antigravity_payload({"remaining_fraction": remaining})
    )

    assert snapshot.windows == []
    assert not snapshot.limit_reached


@pytest.mark.parametrize(
    ("window", "group_name", "window_minutes", "label"),
    [
        ("weekly", "Gemini models", 10_080, "Gemini models"),
        ("5h", "Gemini models", 300, "Gemini models"),
        ("daily", "Gemini models", None, "Gemini models (daily)"),
        ("monthly", "Gemini models", None, "Gemini models (monthly)"),
        ("daily", "", None, "daily"),
    ],
)
def test_parse_antigravity_usage_folds_unknown_window_into_label(
    window: str, group_name: str, window_minutes: int | None, label: str
) -> None:
    snapshot = parse_antigravity_usage(
        _antigravity_payload(
            {"window": window, "remaining_fraction": 0.5},
            group_name=group_name,
        )
    )

    parsed = snapshot.windows[0]
    assert parsed.window == window
    assert parsed.window_minutes == window_minutes
    assert parsed.label == label


def test_parse_antigravity_usage_keeps_same_period_windows_under_group_labels() -> None:
    snapshot = parse_antigravity_usage(_ANTIGRAVITY_USAGE_FIXTURE)
    weekly = [window for window in snapshot.windows if window.window == "weekly"]
    five_hour = [window for window in snapshot.windows if window.window == "5h"]

    assert [window.label for window in weekly] == [
        "Gemini models",
        "Claude and GPT models",
    ]
    assert [window.label for window in five_hour] == [
        "Gemini models",
        "Claude and GPT models",
    ]


def test_parse_antigravity_usage_ignores_text_and_malformed_payloads() -> None:
    for raw in (
        "",
        "Gemini models\tweekly\t0.9\t2026-09-25T07:50:32Z",
        "remaining_fraction=0.5",
        None,
        {"command": {"data": {"groups": "bad"}}},
        {"groups": [{"name": "Gemini models", "buckets": []}]},
    ):
        snapshot = parse_antigravity_usage(raw)
        assert snapshot.windows == []
        assert not snapshot.limit_reached


def _antigravity_payload(
    bucket: dict[str, Any], group_name: str = "Gemini models"
) -> dict[str, Any]:
    group: dict[str, Any] = {"buckets": [{"window": "weekly", **bucket}]}
    if group_name:
        group["name"] = group_name
    return {
        "status": "SUCCESS",
        "command": {"data": {"groups": [group]}},
    }


def test_parse_copilot_quota_reads_measured_snapshots() -> None:
    now = datetime(2026, 9, 18, 23, 42, 5, tzinfo=UTC)
    snapshot = parse_copilot_quota(_COPILOT_QUOTA_FIXTURE, now=now)

    # Unlimited chat / completions have no meter; only the finite budget shows.
    assert snapshot.agent == "copilot"
    assert [
        (w.window, w.used_percent, w.resets_at, w.window_minutes, w.label)
        for w in snapshot.windows
    ] == [
        (
            "premium_interactions",
            30.0,
            "2026-10-01T00:00:00-07:00",
            None,
            "premium_interactions",
        )
    ]
    assert not snapshot.limit_reached
    assert snapshot.checked_at == now.isoformat()


@pytest.mark.parametrize(
    ("remaining", "used", "limit_reached"),
    [(0, 100.0, True), (100, 0.0, False), (68.5, 31.5, False)],
)
def test_parse_copilot_quota_accepts_remaining_percentage_boundaries(
    remaining, used, limit_reached
) -> None:
    snapshot = parse_copilot_quota(_copilot_payload(remainingPercentage=remaining))

    assert [w.used_percent for w in snapshot.windows] == [used]
    assert snapshot.limit_reached is limit_reached


@pytest.mark.parametrize(
    "remaining", [None, "abc", float("nan"), float("inf"), -1, 100.5, True]
)
def test_parse_copilot_quota_drops_unusable_remaining_percentage(remaining) -> None:
    payload = _copilot_payload(remainingPercentage=remaining)
    if remaining is None:
        del payload["quotaSnapshots"]["premium_interactions"]["remainingPercentage"]

    assert parse_copilot_quota(payload).windows == []


@pytest.mark.parametrize(
    "entitlement",
    [
        {"isUnlimitedEntitlement": True, "entitlementRequests": 0},
        {"isUnlimitedEntitlement": False, "entitlementRequests": -1},
    ],
)
def test_parse_copilot_quota_skips_unlimited_entitlements(entitlement) -> None:
    assert parse_copilot_quota(_copilot_payload(**entitlement)).windows == []


def test_parse_copilot_quota_drops_a_reset_that_has_already_passed() -> None:
    """The account API has answered every snapshot with the request's own
    instant as `resetDate` (measured 2026-09-19, in the API's -07:00). A
    reset that is not ahead names no coming reset: the row keeps its usage
    and loses the date, while a reset still ahead is kept as it is."""
    now = datetime(2026, 9, 18, 23, 42, 5, tzinfo=UTC)

    passed = parse_copilot_quota(
        _copilot_payload(resetDate="2026-09-18T16:42:00.213-07:00"), now=now
    )
    coming = parse_copilot_quota(
        _copilot_payload(resetDate="2026-10-01T00:00:00.000-07:00"), now=now
    )

    assert [(w.window, w.resets_at) for w in passed.windows] == [
        ("premium_interactions", "")
    ]
    assert passed.checked_at == now.isoformat()
    assert [w.resets_at for w in coming.windows] == ["2026-10-01T00:00:00-07:00"]


def test_parse_copilot_quota_keeps_unknown_quota_keys_and_missing_reset() -> None:
    payload = _copilot_payload(key="new_budget", resetDate=None)
    del payload["quotaSnapshots"]["new_budget"]["resetDate"]

    snapshot = parse_copilot_quota(payload)

    assert [(w.window, w.label, w.resets_at) for w in snapshot.windows] == [
        ("new_budget", "new_budget", "")
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"quotaSnapshots": []},
        {"quotaSnapshots": {"chat": "text", "": {"remainingPercentage": 5}}},
    ],
)
def test_parse_copilot_quota_tolerates_empty_and_malformed_input(payload) -> None:
    snapshot = parse_copilot_quota(payload)

    assert snapshot.windows == []
    assert not snapshot.limit_reached


def _copilot_payload(key: str = "premium_interactions", **fields: Any) -> dict:
    entry = {
        "isUnlimitedEntitlement": False,
        "entitlementRequests": 300,
        "usedRequests": 90,
        "remainingPercentage": 70,
        "resetDate": "2026-10-01T00:00:00-07:00",
        **fields,
    }
    return {"quotaSnapshots": {key: entry}}
