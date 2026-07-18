"""Account usage snapshots for native AI CLI tools.

Reads the current rate-limit windows (used percent and reset time) from the
tool's own structured interface.  Codex exposes them through the
``account/rateLimits/read`` method of ``codex app-server``; tools without a
structured usage interface simply have no snapshot.

The window parsing is shared with the Codex adapter's pre-turn rate-limit
check so both interpret the provider schema identically.
"""

from __future__ import annotations

import asyncio
import json
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
    """One rate-limit window (e.g. the 5-hour or weekly budget)."""

    window: str
    used_percent: float
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
                window.used_percent >= LIMIT_REACHED_PERCENT
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
    try:
        epoch = int(raw or 0)
    except (TypeError, ValueError):
        return str(raw or "")
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _parse_minutes(raw: Any) -> int | None:
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
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


async def _probe_request(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> Any:
    assert process.stdout is not None
    _probe_send(
        process,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    while True:
        line = await process.stdout.readline()
        if not line:
            raise CliAgentUsageError("Codex App Server closed the stream.")
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
