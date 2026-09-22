"""Account usage per AI CLI tool, as the screens read it.

Usage belongs to the account a tool is logged in with on this device, not to a
member, so every caller asking about one tool shares one probe and one reading.
Each tool is read on its own: a slow or failing tool never holds back another.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    CliAgentUsage,
    CliAgentUsageCheck,
    CliAgentUsageResponse,
    CliAgentUsageWindow,
)
from guildbotics.intelligences.agent_environment.provider_state import has_credentials
from guildbotics.intelligences.agent_runtime.usage import (
    CLI_AGENT_USAGE_READERS,
    CliAgentUsageError,
    CliAgentUsageSnapshot,
    read_cli_agent_usage,
)
from guildbotics.intelligences.cli_agents import cli_agent_info

#: How long a completed probe answers for its tool before the next read
#: refreshes it.
USAGE_TTL_SECONDS = 300.0
#: A failed probe answers for less: the cause is often passing (the host was
#: asleep, the network dropped), and a stale warning should not outlast it.
USAGE_RETRY_SECONDS = 30.0
#: Every probe boots its own environment with the declared resources, so only
#: this many run at once; the rest wait for a slot.
MAX_CONCURRENT_PROBES = 2


def _usage_model(snapshot: CliAgentUsageSnapshot) -> CliAgentUsage:
    return CliAgentUsage(
        agent=snapshot.agent,
        windows=[
            CliAgentUsageWindow(
                window=window.window,
                used_percent=window.used_percent,
                resets_at=window.resets_at,
                window_minutes=window.window_minutes,
                label=window.label,
            )
            for window in snapshot.windows
        ],
        limit_reached=snapshot.limit_reached,
        checked_at=snapshot.checked_at,
    )


@dataclass
class _ToolUsage:
    #: The last successful reading; a failed probe leaves it in place.
    usage: CliAgentUsage | None = None
    #: The latest completed probe, whatever its outcome.
    check: CliAgentUsageCheck | None = None
    #: ``time.monotonic()`` when ``check`` completed.
    checked: float = 0.0
    probe: asyncio.Task[None] | None = None

    def probing(self) -> bool:
        return self.probe is not None and not self.probe.done()


class CliAgentUsageCache:
    """One probe per tool at a time, shared by every reader of that tool."""

    def __init__(self, trace_id: Callable[[], str]) -> None:
        self._trace_id = trace_id
        self._tools: dict[str, _ToolUsage] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    def checks(self) -> dict[str, CliAgentUsageCheck]:
        """The latest completed probe per tool, for settings and alerts."""
        # Status endpoints run in worker threads while probes update the cache.
        return {
            name: tool.check
            for name, tool in self._tools.copy().items()
            if tool.check is not None
        }

    async def read(self, name: str, refresh: bool = False) -> CliAgentUsageResponse:
        """The tool's usage, probing it when due.

        A read waits for the probe only when asked to (``refresh``) or when
        there is no reading to show yet; otherwise it answers with the last
        reading at once and says a newer one is on its way.
        """
        if name not in CLI_AGENT_USAGE_READERS:
            raise AppApiError("validation_error", status_code=422)
        if not has_credentials(cli_agent_info(name)):
            self._tools.pop(name, None)
            return CliAgentUsageResponse(agent=name)
        tool = self._tools.setdefault(name, _ToolUsage())
        due = tool.check is None or time.monotonic() - tool.checked >= (
            USAGE_TTL_SECONDS
            if tool.check.status == "succeeded"
            else USAGE_RETRY_SECONDS
        )
        if (refresh or due) and not tool.probing():
            tool.probe = asyncio.create_task(self._probe(name, tool))
        probe = tool.probe
        if probe is not None and not probe.done() and (refresh or tool.usage is None):
            # A reader that goes away must not cancel the probe others share.
            await asyncio.shield(probe)
        return CliAgentUsageResponse(
            agent=name, usage=tool.usage, check=tool.check, refreshing=tool.probing()
        )

    async def aclose(self) -> None:
        """Cancel running probes so their environments are released."""
        probes = [
            tool.probe
            for tool in self._tools.values()
            if tool.probe is not None and not tool.probe.done()
        ]
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)

    async def _probe(self, name: str, tool: _ToolUsage) -> None:
        usage = None
        async with self._slots:
            try:
                usage = _usage_model(await read_cli_agent_usage(name))
            except CliAgentUsageError as exc:
                logging.getLogger("guildbotics.app_api.cli_agent_usage").warning(
                    "Could not read %s usage: %s", name, exc
                )
        tool.usage = usage or tool.usage
        tool.check = CliAgentUsageCheck(
            status="succeeded" if usage is not None else "failed",
            checked_at=datetime.now(UTC).isoformat(),
            trace_id=self._trace_id(),
        )
        tool.checked = time.monotonic()
