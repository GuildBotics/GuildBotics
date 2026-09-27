"""Account usage snapshots for native AI CLI tools.

Reads the current rate-limit windows (used percent and reset time) from the
tool's own structured interface.  Codex exposes them through the
``account/rateLimits/read`` method of ``codex app-server``; Grok exposes the
billing period, usage percent, and account gate through the ``_x.ai/billing``
and ``_x.ai/auth/check_subscription`` extension requests of ``grok agent stdio``;
Claude Code prints its usage panel headlessly (and without an LLM turn)
through ``claude -p /usage``; Antigravity prints model-group quotas the same
way through ``agy -p /usage --output-format json``; GitHub Copilot answers
``account.getQuota`` on the Copilot SDK server that ``copilot --headless
--stdio`` runs.  Tools without a structured usage interface simply have no
snapshot.

What each tool reports is read by :mod:`.usage_snapshots`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from guildbotics.intelligences.agent_environment.provider_state import (
    masked,
    record_authentication_outcome,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
    EnvironmentProcess,
)
from guildbotics.intelligences.agent_runtime.environment import (
    start_probe_environment,
)
from guildbotics.intelligences.agent_runtime.jsonrpc import CLIENT_INFO
from guildbotics.intelligences.agent_runtime.models import AgentRuntimeError
from guildbotics.intelligences.agent_runtime.usage_snapshots import (
    CliAgentUsageSnapshot,
    parse_antigravity_usage,
    parse_claude_usage,
    parse_codex_rate_limits,
    parse_copilot_quota,
    parse_grok_billing,
)
from guildbotics.intelligences.cli_agents import (
    ANTIGRAVITY_USAGE_COMMAND,
    CLAUDE_USAGE_COMMAND,
    cli_agent_info,
)
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

#: A probe environment boots in seconds. The readers' own timeouts start once
#: it is up, so a boot that stalls (the host slept mid-boot, say) is cut here
#: rather than holding the probe for minutes.
PROBE_START_TIMEOUT_SECONDS = 60.0


class CliAgentUsageError(RuntimeError):
    """The usage snapshot could not be read from the AI CLI tool."""


def _antigravity_command_failure(payload: Any) -> str:
    data = payload if isinstance(payload, dict) else {}
    status = data.get("status")
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("type") or error
    failed = (isinstance(status, str) and status and status != "SUCCESS") or (
        isinstance(error, str) and error
    )
    if not failed:
        return ""
    detail = error if isinstance(error, str) and error else status
    return f"Antigravity /usage failed: {detail}"


async def _probe(
    tool: str, *command: str
) -> tuple[AgentEnvironment, EnvironmentProcess]:
    """Start ``command`` in the tool's probe environment, or say why not."""
    try:
        async with asyncio.timeout(PROBE_START_TIMEOUT_SECONDS):
            environment = await start_probe_environment(tool)
    except AgentRuntimeError as exc:
        raise CliAgentUsageError(str(exc)) from exc
    except TimeoutError as exc:
        raise CliAgentUsageError(
            f"The {cli_agent_info(tool).label} probe environment did not start in time."
        ) from exc
    try:
        process = await environment.run(*command, limit=STREAM_READ_LIMIT)
    except BaseException as exc:
        # Cancellation included: a probe dropped mid-start still releases it.
        await environment.close()
        if isinstance(exc, AgentEnvironmentError):
            raise CliAgentUsageError(f"Could not start {command[0]}: {exc}") from exc
        raise
    return environment, process


async def read_codex_usage(timeout: float = 20.0) -> CliAgentUsageSnapshot:
    """Probe ``codex app-server`` for the current account usage.

    Raises :class:`CliAgentUsageError` when the tool cannot be started, does
    not answer in time, or does not expose the rate-limit capability (e.g.
    API-key providers).
    """
    environment, process = await _probe("codex", "codex", "app-server")
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
        await environment.close()
    return parse_codex_rate_limits(result)


async def read_grok_usage(timeout: float = 20.0) -> CliAgentUsageSnapshot:
    """Probe ``grok agent stdio`` for the current account usage.

    Speaks the ACP handshake with the saved login, then reads the billing
    period and the account gate through Grok's extension requests.  Raises
    :class:`CliAgentUsageError` when the tool cannot be started, has no saved
    login, or does not answer in time.
    """
    # The probe must never let the CLI update itself.
    environment, process = await _probe(
        "grok", "grok", "--no-auto-update", "agent", "stdio"
    )
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
        await environment.close()
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


async def read_antigravity_usage(timeout: float = 30.0) -> CliAgentUsageSnapshot:
    """Probe ``agy -p /usage --output-format json`` for account quotas.

    The slash command is read-only: it starts no agent turn, spends no quota,
    and leaves no conversation. Raises :class:`CliAgentUsageError` when the
    tool cannot be started, exits non-zero, prints no structured JSON, fails
    authentication, or reports no usable quota windows. Accounts that do not
    expose quotas stay unavailable rather than synthesizing 0%.
    """
    stdout, returncode = await _print_output(
        "antigravity",
        *ANTIGRAVITY_USAGE_COMMAND,
        timeout=timeout,
        label="Antigravity",
    )
    if returncode:
        raise CliAgentUsageError(f"Antigravity /usage exited {returncode}.")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CliAgentUsageError("Antigravity printed no usage JSON.") from exc
    failure = _antigravity_command_failure(payload)
    if failure:
        raise CliAgentUsageError(failure)
    snapshot = parse_antigravity_usage(payload)
    if not snapshot.windows:
        raise CliAgentUsageError("Antigravity reported no usage windows.")
    return snapshot


_COPILOT_LABEL = "GitHub Copilot CLI"


async def read_copilot_usage(timeout: float = 30.0) -> CliAgentUsageSnapshot:
    """Probe the Copilot SDK server for the account quota.

    ``copilot --headless --stdio`` serves the Copilot SDK protocol (JSON-RPC
    with Content-Length framing) and signs in with the CLI's own saved
    login, so GuildBotics never reads the credential store. ``connect``
    opens the connection and ``account.getQuota`` answers with the quota
    snapshots. Raises :class:`CliAgentUsageError` when the tool cannot be
    started, does not answer in time, or rejects a request (no saved login,
    or a CLI too old to know the method).
    """
    environment, process = await _probe(
        "copilot", "copilot", "--headless", "--stdio", "--no-auto-update"
    )
    try:
        async with asyncio.timeout(timeout):
            await _probe_request(
                process,
                1,
                "connect",
                {
                    # No task kind is served here: the probe only asks.
                    "supportedTaskKinds": [],
                    "clientInfo": {"editorName": "guildbotics", "editorVersion": "1"},
                },
                label=_COPILOT_LABEL,
                framing=_CONTENT_LENGTH,
            )
            result = await _probe_request(
                process,
                2,
                "account.getQuota",
                {},
                label=_COPILOT_LABEL,
                framing=_CONTENT_LENGTH,
            )
    except TimeoutError as exc:
        raise CliAgentUsageError(f"{_COPILOT_LABEL} did not answer in time.") from exc
    finally:
        await process.kill()
        await environment.close()
    return parse_copilot_quota(result)


async def _print_output(
    tool: str, *command: str, timeout: float, label: str
) -> tuple[bytes, int | None]:
    environment, process = await _probe(tool, *command)
    try:
        async with asyncio.timeout(timeout):
            stdout, _stderr = await process.communicate()
    except TimeoutError as exc:
        raise CliAgentUsageError(f"{label} did not answer in time.") from exc
    finally:
        await process.kill()
        await environment.close()
    return stdout, process.returncode


@dataclass(frozen=True)
class _Framing:
    """How one JSON-RPC message sits on the stream.

    ``frame`` wraps an encoded message for sending; ``read`` returns the next
    message body, or ``b""`` once the stream has ended.
    """

    frame: Callable[[bytes], bytes]
    read: Callable[[EnvironmentProcess], Awaitable[bytes]]


async def _read_line(process: EnvironmentProcess) -> bytes:
    return await process.stdout.readline()


async def _read_content_length(process: EnvironmentProcess) -> bytes:
    """Read one ``Content-Length`` framed body (the LSP / vscode-jsonrpc form)."""
    length = 0
    while True:
        line = await process.stdout.readline()
        if not line:
            return b""
        if not line.strip():
            if length:
                break
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value)
            except ValueError as exc:
                raise CliAgentUsageError(f"Malformed frame header: {line!r}") from exc
    try:
        return await process.stdout.readexactly(length)
    except asyncio.IncompleteReadError:
        return b""


#: Newline-delimited JSON (Codex App Server, ACP).
_JSONL = _Framing(frame=lambda body: body + b"\n", read=_read_line)
#: ``Content-Length`` headers (the Copilot SDK server).
_CONTENT_LENGTH = _Framing(
    frame=lambda body: b"Content-Length: %d\r\n\r\n%s" % (len(body), body),
    read=_read_content_length,
)


async def _probe_request(
    process: EnvironmentProcess,
    request_id: int,
    method: str,
    params: dict[str, Any],
    label: str = "Codex App Server",
    framing: _Framing = _JSONL,
) -> Any:
    await _probe_send(
        process,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        framing,
    )
    while True:
        body = await framing.read(process)
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


async def _probe_send(
    process: EnvironmentProcess, message: dict[str, Any], framing: _Framing = _JSONL
) -> None:
    process.stdin.write(framing.frame(json.dumps(message).encode()))
    await process.stdin.drain()


#: The AI CLI tools with a structured account-usage interface, keyed by their
#: catalog name (:mod:`guildbotics.intelligences.cli_agents`).  Tools absent
#: here have no snapshot and never appear in the usage response.
CLI_AGENT_USAGE_READERS: dict[str, Callable[[], Awaitable[CliAgentUsageSnapshot]]] = {
    "antigravity": read_antigravity_usage,
    "claude": read_claude_usage,
    "codex": read_codex_usage,
    "copilot": read_copilot_usage,
    "grok": read_grok_usage,
}


async def read_cli_agent_usage(name: str) -> CliAgentUsageSnapshot:
    """Read usage and clear auth failure only on windows or an explicit gate.

    Raises:
        CliAgentUsageError: When there is no usage to read; what the tool said
            of it runs where its login is held, so the login is masked in it.
    """
    tool = cli_agent_info(name)
    try:
        snapshot = await CLI_AGENT_USAGE_READERS[name]()
    except CliAgentUsageError as exc:
        raise CliAgentUsageError(masked(tool, str(exc))) from None
    if not snapshot.windows and not snapshot.limit_reached:
        raise CliAgentUsageError(f"{tool.label} reported no usage windows.")
    record_authentication_outcome(tool, failed=False)
    return snapshot
