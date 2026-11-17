"""Read-only provider usage probes inside a private login-state microVM.

Only the normalized snapshot reaches stdout. The Rust host owns the VM;
this process owns and reaps the tool and all subprocesses it starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from guildbotics.intelligences.agent_runtime.usage_snapshots import (
    CliAgentUsageSnapshot,
    parse_claude_usage,
    parse_codex_rate_limits,
    parse_grok_billing,
)
from guildbotics.intelligences.agent_runtime.wire import CLIENT_INFO
from guildbotics.intelligences.cli_agents import (
    CLAUDE_USAGE_COMMAND,
)
from guildbotics.utils.process_limits import STREAM_READ_LIMIT


class CliAgentUsageError(RuntimeError):
    """The tool did not report a usable usage snapshot."""


class EnvironmentProcess:
    """The provider process, stopped as a process group before returning."""

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        assert process.stdin is not None and process.stdout is not None
        self.stdin = process.stdin
        self.stdout = process.stdout

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        self.stdin.close()
        output = await self.stdout.read(STREAM_READ_LIMIT)
        if await self.stdout.read(1):
            raise CliAgentUsageError("The usage response exceeded its size limit")
        await self.process.wait()
        return output, b""

    async def kill(self) -> None:
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()


async def _probe(tool: str, *command: str) -> EnvironmentProcess:
    if tool not in {"codex", "grok", "claude"}:
        raise CliAgentUsageError("Unsupported usage probe")
    return EnvironmentProcess(
        await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=STREAM_READ_LIMIT,
        )
    )


async def read_codex_usage(timeout: float = 20.0) -> CliAgentUsageSnapshot:
    """Probe ``codex app-server`` for the current account usage.

    Raises :class:`CliAgentUsageError` when the tool cannot be started, does
    not answer in time, or does not expose the rate-limit capability (e.g.
    API-key providers).
    """
    process = await _probe("codex", "codex", "app-server")
    try:
        async with asyncio.timeout(timeout):
            await _probe_request(process, 1, "initialize", {"clientInfo": CLIENT_INFO})
            await _probe_send(
                process, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            )
            result = await _probe_request(process, 2, "account/rateLimits/read", {})
    except TimeoutError as exc:
        raise CliAgentUsageError("Codex App Server did not answer in time.") from exc
    finally:
        await process.kill()
    return parse_codex_rate_limits(result)


async def read_grok_usage(timeout: float = 20.0) -> CliAgentUsageSnapshot:
    """Probe ``grok agent stdio`` for the current account usage.

    Speaks the ACP handshake with the saved login, then reads the billing
    period and the account gate through Grok's extension requests.  Raises
    :class:`CliAgentUsageError` when the tool cannot be started, has no saved
    login, or does not answer in time.
    """
    # The probe must never let the CLI update itself.
    process = await _probe("grok", "grok", "--no-auto-update", "agent", "stdio")
    try:
        async with asyncio.timeout(timeout):
            await _probe_request(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": CLIENT_INFO,
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
        await process.kill()
    return parse_grok_billing(billing, subscription)


async def read_claude_usage(timeout: float = 30.0) -> CliAgentUsageSnapshot:
    """Probe ``claude -p /usage`` for the current account usage.

    The ``/usage`` slash command runs headlessly without an LLM turn, so the
    probe consumes no plan quota.  Raises :class:`CliAgentUsageError` when the
    tool cannot be started, does not answer in time, or reports no usage
    lines (e.g. API-key auth, where the plan panel does not exist).
    """
    stdout, _returncode = await _print_output(
        "claude", *CLAUDE_USAGE_COMMAND, timeout=timeout, label="Claude Code"
    )
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


async def _print_output(
    tool: str, *command: str, timeout: float, label: str
) -> tuple[bytes, int | None]:
    process = await _probe(tool, *command)
    try:
        async with asyncio.timeout(timeout):
            stdout, _stderr = await process.communicate()
    except TimeoutError as exc:
        raise CliAgentUsageError(f"{label} did not answer in time.") from exc
    finally:
        await process.kill()
    return stdout, process.returncode


async def _probe_request(
    process: EnvironmentProcess,
    request_id: int,
    method: str,
    params: dict[str, Any],
    label: str = "Codex App Server",
) -> Any:
    await _probe_send(
        process,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    while True:
        body = await process.stdout.readline()
        if not body:
            raise CliAgentUsageError(f"{label} closed the stream.")
        try:
            message = json.loads(body)
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


async def _probe_send(process: EnvironmentProcess, message: dict[str, Any]) -> None:
    process.stdin.write(json.dumps(message).encode() + b"\n")
    await process.stdin.drain()


READERS = {
    "codex": read_codex_usage,
    "grok": read_grok_usage,
    "claude": read_claude_usage,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=sorted(READERS))
    tool = parser.parse_args().tool
    snapshot = asyncio.run(READERS[tool]())
    print(json.dumps(asdict(snapshot)))


if __name__ == "__main__":
    main()
