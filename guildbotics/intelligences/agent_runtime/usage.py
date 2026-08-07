"""Account usage snapshots for native AI CLI tools.

Reads the current rate-limit windows (used percent and reset time) from the
tool's own structured interface.  Codex exposes them through the
``account/rateLimits/read`` method of ``codex app-server``; Grok exposes the
billing period and account gate through the ``_x.ai/billing`` and
``_x.ai/auth/check_subscription`` extension requests of ``grok agent stdio``;
Claude Code prints its usage panel headlessly (and without an LLM turn)
through ``claude -p /usage``.  Tools without a structured usage interface
simply have no snapshot.

The window parsing is shared with the Codex adapter's pre-turn rate-limit
check so both interpret the provider schema identically.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    terminate_process_tree,
)

LIMIT_REACHED_PERCENT = 100.0


class CliAgentUsageError(RuntimeError):
    """The usage snapshot could not be read from the AI CLI tool."""


@dataclass(frozen=True)
class CliAgentUsageWindow:
    """One rate-limit window (e.g. the 5-hour or weekly budget).

    ``used_percent`` is ``None`` for providers that report only the window's
    reset time (e.g. Grok's weekly subscription period).  ``label`` is a
    human-readable qualifier beyond the window duration (e.g. a per-model
    budget's model name).  A ``detail`` window is supplementary: it still
    counts toward the limit state, but the frontend shows it only in the
    expanded usage detail, not as its own meter.
    """

    window: str
    used_percent: float | None = None
    resets_at: str = ""
    window_minutes: int | None = None
    label: str = ""
    detail: bool = False


@dataclass(frozen=True)
class CliAgentUsageSnapshot:
    """Current account usage of one AI CLI tool."""

    agent: str
    windows: list[CliAgentUsageWindow] = field(default_factory=list)
    limit_reached: bool = False
    checked_at: str = ""


def parse_codex_rate_limits(result: Any) -> CliAgentUsageSnapshot:
    """Build a usage snapshot from an ``account/rateLimits/read`` result."""
    data = result if isinstance(result, dict) else {}
    buckets = data.get("rateLimitsByLimitId", data.get("rate_limits_by_limit_id"))
    candidates = list(buckets.values()) if isinstance(buckets, dict) else []
    rate_limits = data.get("rateLimits", data.get("rate_limits"))
    if not candidates and isinstance(rate_limits, dict):
        candidates = [rate_limits]
    windows: list[CliAgentUsageWindow] = []
    limit_reached = False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        limit_reached = limit_reached or bool(
            candidate.get(
                "rateLimitReachedType", candidate.get("rate_limit_reached_type")
            )
        )
        for name in ("primary", "secondary"):
            window = _parse_window(name, candidate.get(name))
            if window is None:
                continue
            windows.append(window)
            limit_reached = limit_reached or (
                (window.used_percent or 0.0) >= LIMIT_REACHED_PERCENT
            )
    return CliAgentUsageSnapshot(
        agent="codex",
        windows=windows,
        limit_reached=limit_reached,
        checked_at=datetime.now(UTC).isoformat(),
    )


def _parse_window(name: str, raw: Any) -> CliAgentUsageWindow | None:
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

    Grok reports no used percent for the subscription quota, so its weekly
    usage period becomes a percentless window carrying only the reset time.
    The on-demand credit budget, when one is configured, does yield a percent.
    An active account gate marks the limit as reached.
    """
    config = _as_dict(_as_dict(billing).get("config"))
    period = _as_dict(config.get("currentPeriod"))
    windows: list[CliAgentUsageWindow] = []
    resets_at = _parse_reset(period.get("end"))
    if resets_at:
        windows.append(
            CliAgentUsageWindow(
                window="subscription",
                resets_at=resets_at,
                window_minutes=_minutes_between(
                    _parse_reset(period.get("start")), resets_at
                ),
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
    limit_reached = bool(
        _as_dict(_as_dict(subscription).get("meta")).get("gate")
    ) or any(
        (window.used_percent or 0.0) >= LIMIT_REACHED_PERCENT for window in windows
    )
    return CliAgentUsageSnapshot(
        agent="grok",
        windows=windows,
        limit_reached=limit_reached,
        checked_at=datetime.now(UTC).isoformat(),
    )


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _decimal_val(raw: Any) -> float:
    if isinstance(raw, dict):
        raw = raw.get("val")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _minutes_between(start_iso: str, end_iso: str) -> int | None:
    if not start_iso:
        return None
    delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None


#: One usage line of the ``/usage`` panel, e.g.
#: ``Current session: 24% used · resets Aug 8 at 11:10am (Asia/Tokyo)``.
_CLAUDE_USAGE_LINE = re.compile(
    r"^(?P<name>[^:\n]+):\s+(?P<percent>\d+(?:\.\d+)?)% used"
    r"(?:\s+·\s+resets\s+(?P<reset>[^\n]+?))?\s*$",
    re.MULTILINE,
)
_CLAUDE_WEEK_MODEL = re.compile(r"^Current week \((?P<model>[^)]+)\)$")
#: ``Aug 8 at 11:10am (Asia/Tokyo)`` / ``Aug 8 at 10am (Asia/Tokyo)``.
_CLAUDE_RESET = re.compile(
    r"^(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s+at\s+"
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
    that cannot be interpreted is dropped rather than guessed.  The session
    and all-models weekly budgets are the summary meters; per-model weekly
    budgets (and any unrecognized budget line) become ``detail`` windows.
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
                detail=True,
            )
        )
    return CliAgentUsageSnapshot(
        agent="claude",
        windows=windows,
        limit_reached=any(
            (window.used_percent or 0.0) >= LIMIT_REACHED_PERCENT for window in windows
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


async def read_codex_usage(
    executable: str = "codex", timeout: float = 20.0
) -> CliAgentUsageSnapshot:
    """Probe ``codex app-server`` for the current account usage.

    Raises :class:`CliAgentUsageError` when the tool cannot be started, does
    not answer in time, or does not expose the rate-limit capability (e.g.
    API-key providers).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=STREAM_READ_LIMIT,
        )
    except OSError as exc:
        raise CliAgentUsageError(f"Could not start Codex App Server: {exc}") from exc
    try:
        async with asyncio.timeout(timeout):
            await _probe_request(
                process,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "guildbotics",
                        "title": "GuildBotics",
                        "version": "1",
                    }
                },
            )
            _probe_send(
                process, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            )
            result = await _probe_request(process, 2, "account/rateLimits/read", {})
    except TimeoutError as exc:
        raise CliAgentUsageError("Codex App Server did not answer in time.") from exc
    finally:
        await terminate_process_tree(process)
    return parse_codex_rate_limits(result)


async def read_grok_usage(
    executable: str = "grok", timeout: float = 20.0
) -> CliAgentUsageSnapshot:
    """Probe ``grok agent stdio`` for the current account usage.

    Speaks the ACP handshake with the saved login, then reads the billing
    period and the account gate through Grok's extension requests.  Raises
    :class:`CliAgentUsageError` when the tool cannot be started, has no saved
    login, or does not answer in time.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            # The probe must never let the CLI update itself.
            "--no-auto-update",
            "agent",
            "stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=STREAM_READ_LIMIT,
        )
    except OSError as exc:
        raise CliAgentUsageError(f"Could not start Grok: {exc}") from exc
    try:
        async with asyncio.timeout(timeout):
            await _probe_request(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    # ACP requires clientInfo.version; Grok rejects the
                    # request when it is absent.
                    "clientInfo": {
                        "name": "guildbotics",
                        "title": "GuildBotics",
                        "version": "1",
                    },
                },
                label="Grok",
            )
            await _probe_request(
                process, 2, "authenticate", {"methodId": "cached_token"}, label="Grok"
            )
            billing = await _probe_request(
                process, 3, "_x.ai/billing", {}, label="Grok"
            )
            subscription = await _probe_request(
                process, 4, "_x.ai/auth/check_subscription", {}, label="Grok"
            )
    except TimeoutError as exc:
        raise CliAgentUsageError("Grok did not answer in time.") from exc
    finally:
        await terminate_process_tree(process)
    return parse_grok_billing(billing, subscription)


async def read_claude_usage(
    executable: str = "claude", timeout: float = 30.0
) -> CliAgentUsageSnapshot:
    """Probe ``claude -p /usage`` for the current account usage.

    The ``/usage`` slash command runs headlessly without an LLM turn, so the
    probe consumes no plan quota.  Raises :class:`CliAgentUsageError` when the
    tool cannot be started, does not answer in time, or reports no usage
    lines (e.g. API-key auth, where the plan panel does not exist).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-p",
            "/usage",
            "--output-format",
            "json",
            # The probe must not pile a resumable session onto disk per poll.
            "--no-session-persistence",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=STREAM_READ_LIMIT,
        )
    except OSError as exc:
        raise CliAgentUsageError(f"Could not start Claude Code: {exc}") from exc
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await process.communicate()
    except TimeoutError as exc:
        raise CliAgentUsageError("Claude Code did not answer in time.") from exc
    finally:
        await terminate_process_tree(process)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CliAgentUsageError("Claude Code printed no usage JSON.") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("is_error"):
        raise CliAgentUsageError(f"Claude Code /usage failed: {result}")
    snapshot = parse_claude_usage(result)
    if not snapshot.windows:
        raise CliAgentUsageError("Claude Code reported no usage windows.")
    return snapshot


async def _probe_request(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict[str, Any],
    label: str = "Codex App Server",
) -> Any:
    assert process.stdout is not None
    _probe_send(
        process,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    while True:
        line = await process.stdout.readline()
        if not line:
            raise CliAgentUsageError(f"{label} closed the stream.")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(message, dict)
            or "method" in message
            or message.get("id") != request_id
        ):
            continue
        if "error" in message:
            raise CliAgentUsageError(str(message["error"]))
        return message.get("result")


def _probe_send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message).encode() + b"\n")


#: The AI CLI tools with a structured account-usage interface, keyed by their
#: catalog name (:mod:`guildbotics.intelligences.cli_agents`).  Tools absent
#: here have no snapshot and never appear in the usage response.
CLI_AGENT_USAGE_READERS: dict[
    str, Callable[[str], Awaitable[CliAgentUsageSnapshot]]
] = {
    "claude": read_claude_usage,
    "codex": read_codex_usage,
    "grok": read_grok_usage,
}
