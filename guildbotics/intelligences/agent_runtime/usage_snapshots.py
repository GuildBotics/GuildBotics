"""Account usage snapshots of native AI CLI tools, read from what each tool
reports: the rate-limit windows (used percent and reset time).

Pure parsing, shared by the usage probes (:mod:`.usage`) on the host and the
Codex adapter's pre-turn rate-limit check inside the command's environment,
so both interpret the provider schema identically.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

LIMIT_REACHED_PERCENT = 100.0


@dataclass(frozen=True)
class CliAgentUsageWindow:
    """One rate-limit window (e.g. the 5-hour or weekly budget).

    ``label`` is a human-readable qualifier beyond the window duration (e.g. a
    per-model budget's model name).
    """

    window: str
    used_percent: float
    resets_at: str = ""
    window_minutes: int | None = None
    label: str = ""


@dataclass(frozen=True)
class CliAgentUsageSnapshot:
    """Current account usage of one AI CLI tool."""

    agent: str
    windows: list[CliAgentUsageWindow] = field(default_factory=list)
    limit_reached: bool = False
    checked_at: str = ""


_CODEX_MAIN_LIMIT_ID = "codex"


def parse_codex_rate_limits(result: Any) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from an ``account/rateLimits/read`` result."""
    data = result if isinstance(result, dict) else {}
    buckets = data.get("rateLimitsByLimitId", data.get("rate_limits_by_limit_id"))
    candidates: list[tuple[Any, Any]] = (
        list(buckets.items()) if isinstance(buckets, dict) else []
    )
    rate_limits = data.get("rateLimits", data.get("rate_limits"))
    if not candidates and isinstance(rate_limits, dict):
        candidates = [(_CODEX_MAIN_LIMIT_ID, rate_limits)]
    main_windows: list[CliAgentUsageWindow] = []
    extra_windows: list[CliAgentUsageWindow] = []
    limit_reached = False
    for bucket_key, candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        limit_reached = limit_reached or bool(
            candidate.get(
                "rateLimitReachedType", candidate.get("rate_limit_reached_type")
            )
        )
        label = _codex_bucket_label(bucket_key, candidate)
        # Main 5h/1w windows lead; extras may arrive first in the payload.
        bucket_windows = extra_windows if label else main_windows
        for name in ("primary", "secondary"):
            window = _parse_window(name, candidate.get(name), label)
            if window is None:
                continue
            bucket_windows.append(window)
            limit_reached = limit_reached or (
                window.used_percent >= LIMIT_REACHED_PERCENT
            )
    return CliAgentUsageSnapshot(
        agent="codex",
        windows=main_windows + extra_windows,
        limit_reached=limit_reached,
        checked_at=datetime.now(UTC).isoformat(),
    )


def _codex_bucket_label(bucket_key: Any, candidate: dict[str, Any]) -> str:
    """Qualify extra Codex buckets so equal-duration windows stay distinct.

    The main ``codex`` bucket keeps an empty label so its 5h/1w meters stay
    unchanged. Extra buckets prefer ``limitName`` (e.g. ``gpt-reserve``) and
    fall back to ``limitId``.
    """
    limit_id = candidate.get("limitId", candidate.get("limit_id"))
    if not isinstance(limit_id, str) or not limit_id:
        limit_id = bucket_key if isinstance(bucket_key, str) else ""
    if limit_id == _CODEX_MAIN_LIMIT_ID:
        return ""
    limit_name = candidate.get("limitName", candidate.get("limit_name"))
    if isinstance(limit_name, str) and limit_name:
        return limit_name
    return limit_id


def _parse_window(name: str, raw: Any, label: str = "") -> CliAgentUsageWindow | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("usedPercent", raw.get("used_percent"))
    if value is None:
        return None
    try:
        used_percent = float(value)
    except (TypeError, ValueError):
        return None
    return CliAgentUsageWindow(
        window=name,
        used_percent=used_percent,
        resets_at=_parse_reset(raw.get("resetsAt", raw.get("resets_at"))),
        window_minutes=_parse_minutes(_first_present(raw, _WINDOW_MINUTES_KEYS)),
        label=label,
    )


# Codex has renamed this field across app-server versions.
_WINDOW_MINUTES_KEYS = (
    "windowDurationMins",
    "window_duration_mins",
    "windowMinutes",
    "window_minutes",
)


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _parse_reset(raw: Any) -> str:
    """Normalize a reset timestamp to ISO, dropping anything unparseable.

    Only valid epoch seconds or ISO timestamps survive; other values become
    "" so downstream date parsing never sees garbage.
    """
    try:
        epoch = int(raw or 0)
    except (TypeError, ValueError):
        if not isinstance(raw, str):
            return ""
        try:
            return datetime.fromisoformat(raw).isoformat()
        except ValueError:
            return ""
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _parse_minutes(raw: Any) -> int | None:
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def parse_grok_billing(billing: Any, subscription: Any) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from Grok's billing and subscription results.

    Grok reports the subscription quota as ``creditUsagePercent``. Accounts
    with unified weekly billing omit that field while the new period is at 0%.
    That one authenticated, ungated shape becomes an explicit 0%; other absent
    or invalid percentages produce no subscription window. When subscription
    usage is available, a configured on-demand credit budget yields a separate
    percent. An active account gate marks the limit as reached.
    """
    config = _as_dict(_as_dict(billing).get("config"))
    period = _as_dict(config.get("currentPeriod"))
    subscription_data = _as_dict(subscription)
    gate = _as_dict(subscription_data.get("meta")).get("gate")
    windows: list[CliAgentUsageWindow] = []
    subscription_percent = _optional_decimal_val(config.get("creditUsagePercent"))
    resets_at = _parse_reset(period.get("end"))
    period_minutes = _minutes_between(_parse_reset(period.get("start")), resets_at)
    if (
        "creditUsagePercent" not in config
        and subscription_data.get("authenticated") is True
        and not gate
        and config.get("isUnifiedBillingUser") is True
        and period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY"
        and period_minutes is not None
    ):
        subscription_percent = 0.0
    if subscription_percent is not None:
        windows.append(
            CliAgentUsageWindow(
                window="subscription",
                used_percent=subscription_percent,
                resets_at=resets_at,
                window_minutes=period_minutes,
            )
        )
        cap = _decimal_val(config.get("onDemandCap"))
        if cap > 0:
            used = _decimal_val(config.get("onDemandUsed"))
            windows.append(
                CliAgentUsageWindow(
                    window="on_demand", used_percent=round(used / cap * 100.0, 1)
                )
            )
    limit_reached = bool(gate) or any(
        window.used_percent >= LIMIT_REACHED_PERCENT for window in windows
    )
    return CliAgentUsageSnapshot(
        agent="grok",
        windows=windows,
        limit_reached=limit_reached,
        checked_at=datetime.now(UTC).isoformat(),
    )


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _optional_decimal_val(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("val")
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _decimal_val(raw: Any) -> float:
    return _optional_decimal_val(raw) or 0.0


def _minutes_between(start_iso: str, end_iso: str) -> int | None:
    if not start_iso or not end_iso:
        return None
    delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None


#: One usage line of the ``/usage`` panel, e.g.
#: ``Current session: 24% used · resets Aug 8, 11:10am (Asia/Tokyo)``.
_CLAUDE_USAGE_LINE = re.compile(
    r"^(?P<name>[^:\n]+):\s+(?P<percent>\d+(?:\.\d+)?)% used"
    r"(?:\s+·\s+resets\s+(?P<reset>[^\n]+?))?\s*$",
    re.MULTILINE,
)
_CLAUDE_WEEK_MODEL = re.compile(r"^Current week \((?P<model>[^)]+)\)$")
#: ``Aug 8, 11:10am (Asia/Tokyo)`` / ``Aug 8, 10am (Asia/Tokyo)``.
_CLAUDE_RESET = re.compile(
    r"^(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)"
    r"(?:\s+\((?P<tz>[^)]+)\))?$"
)
_CLAUDE_SESSION_MINUTES = 300
_CLAUDE_WEEK_MINUTES = 10_080


def parse_claude_usage(
    result: Any, now: datetime | None = None
) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from the ``claude -p /usage`` result text.

    The panel is text, so parsing is tolerant: only lines shaped like
    ``<name>: <n>% used[ · resets <time>]`` become windows, and a reset time
    that cannot be interpreted is dropped rather than guessed.  An
    unrecognized budget line keeps its name as the label and has no known
    period.
    """
    text = result if isinstance(result, str) else ""
    windows: list[CliAgentUsageWindow] = []
    for match in _CLAUDE_USAGE_LINE.finditer(text):
        name = match.group("name").strip()
        used_percent = float(match.group("percent"))
        resets_at = _parse_claude_reset(match.group("reset") or "", now)
        if name == "Current session":
            windows.append(
                CliAgentUsageWindow(
                    window="session",
                    used_percent=used_percent,
                    resets_at=resets_at,
                    window_minutes=_CLAUDE_SESSION_MINUTES,
                )
            )
            continue
        if name == "Current week (all models)":
            windows.append(
                CliAgentUsageWindow(
                    window="week",
                    used_percent=used_percent,
                    resets_at=resets_at,
                    window_minutes=_CLAUDE_WEEK_MINUTES,
                )
            )
            continue
        model = _CLAUDE_WEEK_MODEL.match(name)
        windows.append(
            CliAgentUsageWindow(
                window=re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
                used_percent=used_percent,
                resets_at=resets_at,
                window_minutes=_CLAUDE_WEEK_MINUTES if model else None,
                label=model.group("model") if model else name,
            )
        )
    return CliAgentUsageSnapshot(
        agent="claude",
        windows=windows,
        limit_reached=any(
            window.used_percent >= LIMIT_REACHED_PERCENT for window in windows
        ),
        checked_at=datetime.now(UTC).isoformat(),
    )


def _parse_claude_reset(raw: str, now: datetime | None = None) -> str:
    """Interpret a ``/usage`` reset phrase as an ISO timestamp, or ``""``.

    The phrase carries no year, so the nearest future occurrence wins; a
    missing or unknown timezone makes the instant ambiguous, so the reset is
    dropped instead of guessed.
    """
    match = _CLAUDE_RESET.match(raw.strip())
    if match is None or not match.group("tz"):
        return ""
    try:
        zone = ZoneInfo(match.group("tz"))
        month = datetime.strptime(match.group("month")[:3], "%b").month
    except (KeyError, ValueError):
        return ""
    hour = int(match.group("hour")) % 12
    if match.group("ampm") == "pm":
        hour += 12
    current = now.astimezone(zone) if now else datetime.now(zone)
    try:
        reset = datetime(
            current.year,
            month,
            int(match.group("day")),
            hour,
            int(match.group("minute") or 0),
            tzinfo=zone,
        )
    except ValueError:
        return ""
    if reset < current - timedelta(days=1):
        reset = reset.replace(year=current.year + 1)
    return reset.isoformat()


_ANTIGRAVITY_WINDOW_MINUTES = {
    "weekly": _CLAUDE_WEEK_MINUTES,
    "5h": _CLAUDE_SESSION_MINUTES,
}


def parse_antigravity_usage(result: Any) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from ``agy -p /usage --output-format json``.

    The measured 1.2.13 payload keeps quotas in
    ``command.data.groups[].buckets[]``. Each bucket's ``remaining_fraction``
    (1 remaining means unused) becomes
    ``used_percent = (1 - remaining_fraction) * 100``. Missing, non-numeric,
    non-finite, or out-of-range fractions are dropped rather than synthesized
    as 0% or 100%. Known windows (``weekly``, ``5h``) get a duration in
    minutes; any other non-empty ``window`` stays as a row and folds the raw
    value into ``label`` so the Activity view can still tell periods apart.
    TUI, status-line, and tab-separated text are not parsed.
    """
    command = _as_dict(_as_dict(result).get("command"))
    groups = _as_dict(command.get("data")).get("groups")
    windows: list[CliAgentUsageWindow] = []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            label = _first_text(group, ("name", "title", "label", "id"))
            buckets = group.get("buckets")
            if not isinstance(buckets, list):
                continue
            for bucket in buckets:
                window = _parse_antigravity_bucket(bucket, label)
                if window is not None:
                    windows.append(window)
    return CliAgentUsageSnapshot(
        agent="antigravity",
        windows=windows,
        limit_reached=any(
            window.used_percent >= LIMIT_REACHED_PERCENT for window in windows
        ),
        checked_at=datetime.now(UTC).isoformat(),
    )


def _parse_antigravity_bucket(raw: Any, label: str) -> CliAgentUsageWindow | None:
    if not isinstance(raw, dict):
        return None
    window = raw.get("window")
    if not isinstance(window, str) or not window:
        return None
    remaining = _unit_fraction(raw.get("remaining_fraction"))
    if remaining is None:
        return None
    window_minutes = _ANTIGRAVITY_WINDOW_MINUTES.get(window)
    return CliAgentUsageWindow(
        window=window,
        used_percent=(1.0 - remaining) * 100.0,
        resets_at=_parse_reset(raw.get("reset_time")),
        window_minutes=window_minutes,
        label=_antigravity_window_label(label, window, window_minutes),
    )


def _antigravity_window_label(
    label: str, window: str, window_minutes: int | None
) -> str:
    """Keep unknown periods distinguishable without guessing their duration."""
    if window_minutes is not None:
        return label
    return f"{label} ({window})" if label else window


def _first_text(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _unit_fraction(raw: Any) -> float | None:
    """Return a finite fraction in ``[0, 1]``, else ``None``."""
    return _bounded(raw, 1.0)


def _bounded(raw: Any, upper: float) -> float | None:
    """Return a finite number in ``[0, upper]``, else ``None``.

    Booleans are excluded so ``True``/``False`` never become 100%/0%.
    """
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0 or value > upper:
        return None
    return value


def parse_copilot_quota(
    result: Any, now: datetime | None = None
) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from an ``account.getQuota`` result.

    ``quotaSnapshots`` is keyed by quota type (``premium_interactions``,
    ``chat``, ``completions``, ...). The keys are runtime strings, so every
    finite entitlement becomes a window labelled with its key instead of
    being matched against a list: ``used_percent`` is
    ``100 - remainingPercentage`` and ``resetDate`` is the reset time when
    it lies ahead of ``now``. The account API has answered with the
    request's own instant as the reset date of every snapshot, and a reset
    that has already passed names no coming reset, so it is dropped rather
    than shown as one. An unlimited entitlement (``isUnlimitedEntitlement``,
    or a negative ``entitlementRequests``) has nothing to meter and is
    skipped, as is a snapshot whose ``remainingPercentage`` is missing,
    non-numeric, non-finite, or outside 0-100. The period length is not
    reported, so no window duration is guessed from the reset date. An
    exhausted quota marks the limit as reached.
    """
    checked = now or datetime.now(UTC)
    snapshots = _as_dict(_as_dict(result).get("quotaSnapshots"))
    windows = [
        window
        for key, raw in snapshots.items()
        if (window := _parse_copilot_snapshot(key, raw, checked)) is not None
    ]
    return CliAgentUsageSnapshot(
        agent="copilot",
        windows=windows,
        limit_reached=any(
            window.used_percent >= LIMIT_REACHED_PERCENT for window in windows
        ),
        checked_at=checked.isoformat(),
    )


def _coming_reset(raw: Any, checked: datetime) -> str:
    """The reset time as ISO, or "" unless it lies ahead of ``checked``."""
    reset = _parse_reset(raw)
    if not reset:
        return ""
    at = datetime.fromisoformat(reset)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return reset if at > checked else ""


def _parse_copilot_snapshot(
    key: Any, raw: Any, checked: datetime
) -> CliAgentUsageWindow | None:
    if not isinstance(key, str) or not key or not isinstance(raw, dict):
        return None
    entitlement = raw.get("entitlementRequests")
    unlimited = raw.get("isUnlimitedEntitlement") is True or (
        isinstance(entitlement, int | float)
        and not isinstance(entitlement, bool)
        and entitlement < 0
    )
    remaining = _bounded(raw.get("remainingPercentage"), LIMIT_REACHED_PERCENT)
    if unlimited or remaining is None:
        return None
    return CliAgentUsageWindow(
        window=key,
        used_percent=LIMIT_REACHED_PERCENT - remaining,
        resets_at=_coming_reset(raw.get("resetDate"), checked),
        label=key,
    )
