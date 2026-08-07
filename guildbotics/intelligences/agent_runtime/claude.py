"""Claude Code stream-json adapter."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from logging import getLogger
from typing import Any

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    isolated_agent_environment,
    member_command_environment,
    remove_isolated_config,
    terminate_process_tree,
)
from guildbotics.intelligences.agent_runtime.models import (
    SETTINGS_SCOPE_SESSION,
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationRecord,
    EventSink,
)
from guildbotics.runtime.person_lease import delegation_environment

_PERMISSION_MODE = "bypassPermissions"
_SESSION_SETTINGS = json.dumps({"sandbox": {"enabled": False}}, separators=(",", ":"))
# A read-only turn runs under the ordinary permission mode, where anything not
# allowed below is denied outright in non-interactive mode. The allowlist is the
# enforcement; the prompt is only a description of it.
_READ_ONLY_PERMISSION_MODE = "default"
_READ_ONLY_ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Bash(guildbotics diagnostics:*)",
)
_READ_ONLY_DISALLOWED_TOOLS = (
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)
#: The effort-mapping keys Claude Code can act on: the model it runs and the
#: effort level it runs at.
_EFFORT_SETTING_KEYS = frozenset({"model", "effort"})
#: The levels `claude --help` advertises for `--effort`.
_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh", "max"})
_LOGGER = getLogger(__name__)
_PROCESS_EXIT_GRACE_SECONDS = 2.0
_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0
_SESSION_LIMIT_PATTERN = re.compile(
    r"\AYou've hit your session limit"
    r"(?:\s*·\s*(?P<retry_after_text>resets\s+\d{1,2}:\d{2}\s*(?:am|pm)"
    r"\s+\((?P<retry_after_timezone>[^()\r\n]+)\)))?\s*\Z",
    re.IGNORECASE,
)


class ClaudeStreamJsonAdapter:
    name = "claude-stream-json"
    # Claude Code fixes its model and effort when the session starts; a resumed
    # session cannot be reconfigured, so a settings change rotates the
    # conversation.
    settings_scope = SETTINGS_SCOPE_SESSION

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        return _applied_effort_settings(context)

    def __init__(
        self,
        *,
        executable: str = "claude",
        timeout: float = 3600.0,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._capabilities_checked = False

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        await self._ensure_supported(context)
        env, gh_config_dir = isolated_agent_environment(context.cwd)
        env.update(member_command_environment(context))
        env.update(delegation_environment(context.run_id))
        permission_mode = (
            _READ_ONLY_PERMISSION_MODE if context.read_only else _PERMISSION_MODE
        )
        args = [
            self._executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",
            "--settings",
            _SESSION_SETTINGS,
            "--permission-mode",
            permission_mode,
        ]
        if context.read_only:
            args.extend(("--allowed-tools", *_READ_ONLY_ALLOWED_TOOLS))
            args.extend(("--disallowed-tools", *_READ_ONLY_DISALLOWED_TOOLS))
        _warn_unusable_effort_settings(context)
        args.extend(_effort_arguments(context))
        if conversation.provider_session_id:
            args.extend(("--resume", conversation.provider_session_id))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(context.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_READ_LIMIT,
            )
        except OSError as exc:
            remove_isolated_config(gh_config_dir)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"Could not start Claude Code: {exc}",
            ) from exc
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        stderr_task = (
            asyncio.create_task(process.stderr.read())
            if process.stderr is not None
            else None
        )
        events: list[AgentEvent] = []
        session_id = conversation.provider_session_id
        reported_model = ""
        terminal_output = ""
        usage: dict[str, int] = {}
        terminal_seen = False
        retry_error: AgentRuntimeError | None = None
        active_items: dict[str, AgentEvent] = {}
        input_message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        process.stdin.write(
            (json.dumps(input_message, ensure_ascii=False) + "\n").encode()
        )
        policy_event = AgentEvent(
            AgentEventKind.APPROVAL,
            "policy",
            approval=permission_mode,
            details={"bash_sandbox": False, "read_only": context.read_only},
        )
        events.append(policy_event)
        emitted = emit(policy_event)
        if asyncio.iscoroutine(emitted):
            await emitted
        await process.stdin.drain()
        process.stdin.close()
        observed_returncode: int | None = None
        try:
            async with asyncio.timeout(self._timeout):
                while line := await process.stdout.readline():
                    try:
                        raw = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise AgentRuntimeError(
                            AgentRuntimeErrorCategory.PROTOCOL,
                            f"Malformed Claude stream-json event: {exc}",
                            rotate_session=True,
                        ) from exc
                    if not isinstance(raw, dict):
                        continue
                    session_id = str(raw.get("session_id", "") or session_id)
                    reported_model = _reported_model(raw) or reported_model
                    error = _structured_error(raw) or _rate_limit_event_error(raw)
                    if error is not None:
                        if error.category is AgentRuntimeErrorCategory.RATE_LIMITED:
                            # An error naming the exact reset instant wins over
                            # one that only knows the CLI's next retry delay.
                            if retry_error is None or "retry_after_at" in error.details:
                                retry_error = error
                        else:
                            raise error
                    for decoded in _decode_events(raw, session_id):
                        event = decoded
                        if event.item_id and event.name == "started":
                            active_items[event.item_id] = event
                        elif event.item_id and event.name == "completed":
                            started = active_items.pop(event.item_id, None)
                            if started is not None:
                                event = replace(
                                    event,
                                    kind=started.kind,
                                    command=started.command,
                                    path=started.path,
                                    details={**started.details, **event.details},
                                )
                        events.append(event)
                        result = emit(event)
                        if asyncio.iscoroutine(result):
                            await result
                    if raw.get("type") == "result":
                        terminal_seen = True
                        terminal_output = str(raw.get("result", "") or "").strip()
                        usage = _usage(raw.get("usage"))
                        if bool(raw.get("is_error")):
                            if retry_error is not None:
                                raise retry_error
                            if session_limit := _session_limit_error(terminal_output):
                                raise session_limit
                            raise AgentRuntimeError(
                                AgentRuntimeErrorCategory.PROCESS,
                                terminal_output
                                or "Claude Code returned an error result.",
                                details={"subtype": raw.get("subtype")},
                                rotate_session=True,
                            )
                        break
        except TimeoutError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Claude Code turn timed out.",
                rotate_session=True,
            ) from exc
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        except AgentRuntimeError:
            raise
        except ValueError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                f"Claude Code stream-json output could not be read: {exc}",
                rotate_session=True,
            ) from exc
        finally:
            stderr = ""
            if process.returncode is None:
                with suppress(Exception):
                    await asyncio.wait_for(
                        process.wait(), timeout=_PROCESS_EXIT_GRACE_SECONDS
                    )
            observed_returncode = process.returncode
            await terminate_process_tree(process)
            if stderr_task is not None:
                try:
                    stderr = (
                        await asyncio.wait_for(
                            stderr_task, timeout=_PIPE_DRAIN_TIMEOUT_SECONDS
                        )
                    ).decode(errors="replace")
                except TimeoutError:
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task
            remove_isolated_config(gh_config_dir)
            self._process = None
        # A non-error terminal result is authoritative. Any later negative exit
        # status can be caused by our cleanup of a CLI that is still waiting for
        # background descendants, and must not discard the valid response or
        # rotate its resumable session.
        returncode = 0
        if not terminal_seen:
            returncode = (
                observed_returncode
                if observed_returncode is not None
                else (process.returncode or 0)
            )
        if returncode != 0:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"Claude Code exited with code {returncode}: {stderr.strip() or 'no output'}",
                details={"returncode": returncode},
                rotate_session=True,
            )
        if not terminal_seen:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Claude Code stream ended without a terminal result event.",
                rotate_session=True,
            )
        if not session_id:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Claude Code returned no session id.",
                rotate_session=True,
            )
        if not terminal_output:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Claude Code completed without a terminal response.",
                rotate_session=True,
            )
        return AgentTerminalResult(
            output=terminal_output,
            events=tuple(events),
            provider_session_id=session_id,
            finish_reason="completed",
            usage=usage,
            stderr=stderr.strip(),
            returncode=returncode,
            model=reported_model,
            # A turn that imposed nothing left the session on the settings it
            # already ran with, which the conversation remembers.
            effort=_applied_effort(context) or conversation.effective_effort,
        )

    async def interrupt(self) -> None:
        if self._process is not None and self._process.returncode is None:
            await terminate_process_tree(self._process)

    async def close(self) -> None:
        await self.interrupt()

    async def _ensure_supported(self, context: AgentExecutionContext) -> None:
        if self._capabilities_checked:
            return
        env, gh_config_dir = isolated_agent_environment(context.cwd)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                "--help",
                cwd=str(context.cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError) as exc:
            if process is not None and process.returncode is None:
                await terminate_process_tree(process)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                f"Could not inspect Claude Code stream-json capabilities: {exc}",
            ) from exc
        finally:
            remove_isolated_config(gh_config_dir)
        help_text = (stdout + stderr).decode(errors="replace")
        required = ("--input-format", "--output-format", "stream-json", "--resume")
        missing = [flag for flag in required if flag not in help_text]
        if process.returncode != 0 or missing:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                "The installed Claude Code version does not expose the required "
                "stream-json and exact-resume capabilities.",
                details={"missing_capabilities": missing},
            )
        self._capabilities_checked = True


def _applied_effort_settings(context: AgentExecutionContext) -> dict[str, Any]:
    """The effort settings this adapter can really impose, normalized.

    Silent by design: it also backs the session fingerprint, which is computed
    outside the run path and must not emit a second round of warnings.
    """
    settings: dict[str, Any] = {}
    if model := str(context.provider_options.get("model", "") or "").strip():
        settings["model"] = model
    effort = str(context.provider_options.get("effort", "") or "").strip().lower()
    if effort in _EFFORT_VALUES:
        settings["effort"] = effort
    return settings


def _applied_effort(context: AgentExecutionContext) -> str:
    """The effort level this turn really imposed on the session.

    Claude Code reports no effort of its own, so the only honest value is the
    level the adapter actually put on the command line, in Claude Code's own
    vocabulary. A turn that imposes nothing leaves the session on the settings
    it already has, and must not claim a level it never applied.
    """
    return str(_applied_effort_settings(context).get("effort", ""))


def _reported_model(raw: dict[str, Any]) -> str:
    """The model Claude Code names for the session, from its init event."""
    if raw.get("type") != "system" or raw.get("subtype") != "init":
        return ""
    return str(raw.get("model", "") or "")


def _effort_arguments(context: AgentExecutionContext) -> list[str]:
    """Translate the turn's effort settings into Claude Code CLI flags."""
    settings = _applied_effort_settings(context)
    args: list[str] = []
    if model := str(settings.get("model", "")):
        args.extend(("--model", model))
    if effort := str(settings.get("effort", "")):
        args.extend(("--effort", effort))
    return args


def _warn_unusable_effort_settings(context: AgentExecutionContext) -> None:
    """Report requested settings this adapter cannot act on."""
    unknown = sorted(set(context.provider_options) - _EFFORT_SETTING_KEYS)
    if unknown:
        _LOGGER.warning(
            "Ignoring unsupported Claude Code effort settings: %s", ", ".join(unknown)
        )
    effort = str(context.provider_options.get("effort", "") or "").strip()
    if effort and "effort" not in _applied_effort_settings(context):
        _LOGGER.warning("Ignoring unsupported Claude Code effort '%s'.", effort)


def _structured_error(raw: dict[str, Any]) -> AgentRuntimeError | None:
    if raw.get("type") != "system" or raw.get("subtype") != "api_retry":
        return None
    category = str(raw.get("error", "") or "")
    details = {
        "attempt": raw.get("attempt"),
        "max_retries": raw.get("max_retries"),
        "retry_delay_ms": raw.get("retry_delay_ms"),
        "error_status": raw.get("error_status"),
    }
    if category in {"authentication_failed", "oauth_org_not_allowed"}:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            "Claude Code authentication failed.",
            details=details,
            rotate_session=True,
        )
    if category == "rate_limit":
        try:
            delay = max(0, int(raw.get("retry_delay_ms", 0) or 0))
        except (TypeError, ValueError):
            delay = 0
        details["retry_after_seconds"] = delay / 1000
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.RATE_LIMITED,
            "Claude Code rate limit is active.",
            details=details,
            rotate_session=False,
        )
    return None


#: ``rate_limit_info`` fields worth carrying into details, snake_cased.
_RATE_LIMIT_INFO_KEYS = (
    ("rateLimitType", "rate_limit_type"),
    ("utilization", "utilization"),
    ("overageStatus", "overage_status"),
    ("isUsingOverage", "is_using_overage"),
)


def _rate_limit_info(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("type") != "rate_limit_event":
        return {}
    info = raw.get("rate_limit_info")
    return info if isinstance(info, dict) else {}


def _rate_limit_event_details(info: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {"status": str(info.get("status", "") or "")}
    for source, target in _RATE_LIMIT_INFO_KEYS:
        if source in info:
            details[target] = info[source]
    if resets_at := _epoch_to_iso(info.get("resetsAt")):
        details["retry_after_at"] = resets_at
    return details


def _rate_limit_event_error(raw: dict[str, Any]) -> AgentRuntimeError | None:
    """Normalize a non-allowed unified rate-limit signal to the shared error.

    Claude Code derives ``rate_limit_event`` from the API's unified rate-limit
    response headers at the start of every turn.  A non-allowed status names
    the binding window and its exact reset instant, which downstream retry
    scheduling prefers over the parsed human text of the terminal message.
    """
    info = _rate_limit_info(raw)
    status = str(info.get("status", "") or "")
    if not status or status == "allowed":
        return None
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.RATE_LIMITED,
        "Claude Code rate limit is active.",
        details=_rate_limit_event_details(info),
        rotate_session=False,
    )


def _epoch_to_iso(raw: Any) -> str:
    try:
        epoch = int(raw or 0)
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _session_limit_error(message: str) -> AgentRuntimeError | None:
    """Normalize Claude Code's terminal subscription-limit result."""
    match = _SESSION_LIMIT_PATTERN.fullmatch(message)
    if match is None:
        return None
    details = {
        key: value
        for key in ("retry_after_text", "retry_after_timezone")
        if (value := str(match.group(key) or "").strip())
    }
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.RATE_LIMITED,
        "Claude Code session limit is active.",
        details=details,
        rotate_session=False,
    )


def _decode_events(raw: dict[str, Any], session_id: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    event_type = str(raw.get("type", ""))
    subtype = str(raw.get("subtype", ""))
    if event_type == "system" and subtype == "init":
        events.append(
            AgentEvent(
                AgentEventKind.PROCESS,
                "initialized",
                provider_session_id=session_id,
                details={"model": raw.get("model"), "tools": raw.get("tools", [])},
            )
        )
    if event_type == "system" and subtype == "compact_boundary":
        events.append(
            AgentEvent(
                AgentEventKind.TURN,
                "context_compaction",
                provider_session_id=session_id,
                details={
                    "provider_event": subtype,
                    "compact_metadata": raw.get("compact_metadata", {}),
                },
            )
        )
    # Every turn opens with an allowed rate_limit_event; only the ones that
    # carry information beyond "still fine" — a utilization reading or a
    # non-allowed status — earn a diagnostics record.
    if info := _rate_limit_info(raw):
        status = str(info.get("status", "") or "")
        if status != "allowed" or "utilization" in info:
            events.append(
                AgentEvent(
                    AgentEventKind.TURN,
                    "rate_limit_status",
                    provider_session_id=session_id,
                    details=_rate_limit_event_details(info),
                )
            )
    if event_type == "stream_event":
        event: dict[str, Any] = (
            raw["event"] if isinstance(raw.get("event"), dict) else {}
        )
        delta: dict[str, Any] = (
            event["delta"] if isinstance(event.get("delta"), dict) else {}
        )
        if delta.get("type") == "text_delta":
            events.append(
                AgentEvent(
                    AgentEventKind.ASSISTANT,
                    "delta",
                    message=str(delta.get("text", "") or ""),
                    provider_session_id=session_id,
                )
            )
    if event_type == "assistant":
        message: dict[str, Any] = (
            raw["message"] if isinstance(raw.get("message"), dict) else {}
        )
        content: list[Any] = (
            message["content"] if isinstance(message.get("content"), list) else []
        )
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_name = str(block.get("name", "") or "")
                tool_input = (
                    block["input"] if isinstance(block.get("input"), dict) else {}
                )
                path = _tool_path(tool_input)
                if tool_name in {"Edit", "MultiEdit", "NotebookEdit", "Write"}:
                    kind = AgentEventKind.FILE_CHANGE
                elif tool_name == "Bash":
                    kind = AgentEventKind.COMMAND
                else:
                    kind = AgentEventKind.TOOL
                events.append(
                    AgentEvent(
                        kind,
                        "started",
                        provider_session_id=session_id,
                        item_id=str(block.get("id", "") or ""),
                        command=str(tool_input.get("command", "") or tool_name),
                        path=path,
                        details={"tool": tool_name},
                    )
                )
            elif block.get("type") == "text":
                events.append(
                    AgentEvent(
                        AgentEventKind.ASSISTANT,
                        "completed",
                        message=str(block.get("text", "") or ""),
                        provider_session_id=session_id,
                    )
                )
    if event_type == "user":
        user_message: dict[str, Any] = (
            raw["message"] if isinstance(raw.get("message"), dict) else {}
        )
        user_content: list[Any] = (
            user_message["content"]
            if isinstance(user_message.get("content"), list)
            else []
        )
        for block in user_content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(
                    AgentEvent(
                        AgentEventKind.TOOL,
                        "completed",
                        message=_content_text(block.get("content")),
                        provider_session_id=session_id,
                        item_id=str(block.get("tool_use_id", "") or ""),
                        details={"is_error": bool(block.get("is_error"))},
                    )
                )
    if event_type == "result":
        events.append(
            AgentEvent(
                AgentEventKind.TURN,
                "completed",
                message=str(raw.get("result", "") or ""),
                provider_session_id=session_id,
                usage=_usage(raw.get("usage")),
                details={"subtype": subtype, "is_error": bool(raw.get("is_error"))},
            )
        )
    return events


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text", "")) for item in value if isinstance(item, dict)
        )
    return ""


def _tool_path(tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _usage(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    output: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if key not in raw:
            continue
        try:
            output[key] = max(0, int(raw.get(key, 0) or 0))
        except (TypeError, ValueError):
            continue
    return output
