"""Account usage snapshots for native AI CLI tools.

Reads the current rate-limit windows (used percent and reset time) from the
tool's own structured interface.  Codex exposes them through the
``account/rateLimits/read`` method of ``codex app-server``; Grok exposes the
billing period and account gate through the ``_x.ai/billing`` and
``_x.ai/auth/check_subscription`` extension requests of ``grok agent stdio``.
Tools without a structured usage interface simply have no snapshot.

The window parsing is shared with the Codex adapter's pre-turn rate-limit
check so both interpret the provider schema identically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
    reset time (e.g. Grok's weekly subscription period).
    """

    window: str
    used_percent: float | None = None
    resets_at: str = ""
    window_minutes: int | None = None


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
    "codex": read_codex_usage,
    "grok": read_grok_usage,
}
