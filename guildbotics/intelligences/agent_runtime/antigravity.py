"""Antigravity (``agy``) stream-json adapter.

``agy`` has no resident server mode: the only programmatic entry point is a
single ``--print`` run. One turn is therefore one process, and session identity
is carried by ``--conversation <id>`` rather than by a living process.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import suppress
from logging import getLogger
from pathlib import Path
from typing import Any

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    isolated_agent_environment,
    member_command_environment,
    remove_isolated_config,
    terminate_process_tree,
)
from guildbotics.intelligences.agent_runtime.models import (
    SETTINGS_SCOPE_TURN,
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

_LOGGER = getLogger(__name__)

#: The effort-mapping keys ``agy`` can act on. They are mutually exclusive on
#: the command line: every catalogued model either embeds its effort tier in the
#: id (``gemini-3.6-flash-low``) or rejects ``--effort`` outright
#: (``claude-sonnet-4-6``), and passing both makes ``agy`` refuse the turn.
_EFFORT_SETTING_KEYS = frozenset({"model", "effort"})
_EFFORT_VALUES = frozenset({"low", "medium", "high"})

#: ``agy`` resolves its tools against the directories it was told to work in,
#: not against the process working directory. Without this flag every tool --
#: including ``run_command`` -- runs in the CLI's own scratch directory instead
#: of the member's workspace.
_WORKSPACE_FLAG = "--add-dir"

_MODELS_TIMEOUT_SECONDS = 10.0
_HELP_TIMEOUT_SECONDS = 10.0
_PROCESS_EXIT_GRACE_SECONDS = 2.0
_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0
#: Time ``agy``'s own ``--print-timeout`` is given beyond the adapter budget, so
#: the CLI reports a structured timeout result before the outer watchdog fires.
_TIMEOUT_GRACE_SECONDS = 60.0
#: The prompt travels in ``argv`` because ``--print`` requires its value there.
#: Linux caps a single argv string at MAX_ARG_STRLEN (32 pages, 128 KiB) -- the
#: tightest limit among the supported platforms -- so anything larger would die
#: in ``execve`` with an opaque ``E2BIG`` instead of this explicit error.
_MAX_PROMPT_BYTES = 120 * 1024
#: How much of ``agy``'s own log file is attached to a failure.
_LOG_TAIL_BYTES = 4 * 1024

#: The tools that change files, so their steps read as file changes rather than
#: as anonymous tool calls.
_FILE_CHANGE_TOOLS = frozenset(
    {
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
        "notebook_edit",
    }
)
_COMMAND_TOOLS = frozenset({"run_command", "send_command_input", "command_status"})
#: Where a tool step keeps the path it works on, by tool parameter name.
_PATH_PARAMETERS = ("TargetFile", "AbsolutePath", "DirectoryPath", "SearchPath")

#: Last-resort classification of a terminal ``error`` string. ``agy`` reports
#: quota and credential failures as prose, so until a structured code shows up
#: in a real exhaustion these anchored patterns are the actual guard. Add a
#: fixture the first time a genuine payload is observed.
_RATE_LIMIT_PATTERN = re.compile(
    r"RESOURCE_EXHAUSTED|Individual quota reached|quota exceeded", re.IGNORECASE
)
_AUTHENTICATION_PATTERN = re.compile(
    r"401 Unauthorized|UNAUTHENTICATED|invalid_grant"
    r"|authentication(?:_error| failed| required)|token (?:expired|revoked)",
    re.IGNORECASE,
)
_RETRY_AFTER_PATTERN = re.compile(
    r"Resets in\s+(?:\d+h)?(?:\d+m)?(?:\d+s)?", re.IGNORECASE
)


class AntigravityStreamJsonAdapter:
    name = "antigravity-stream-json"
    # Every turn is its own process and carries `--model` / `--effort` on its
    # own command line, and a resumed conversation honours a changed model, so
    # a settings change never needs a fresh conversation.
    settings_scope = SETTINGS_SCOPE_TURN

    def __init__(
        self,
        *,
        executable: str = "agy",
        timeout: float = 3600.0,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._capabilities_checked = False
        self._model_catalog: frozenset[str] = frozenset()
        self._model_catalog_read = False

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        """The effort settings this adapter recognizes.

        Not used for rotation (this adapter is turn-scoped, so a change costs
        nothing), but it keeps the contract uniform across adapters.
        """
        return _requested_settings(context)

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        await self._ensure_supported(context)
        prompt_bytes = len(prompt.encode())
        if prompt_bytes > _MAX_PROMPT_BYTES:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "The prompt is too long for Antigravity, which only accepts it "
                "as a command-line argument.",
                details={"prompt_bytes": prompt_bytes, "limit": _MAX_PROMPT_BYTES},
            )
        settings, rejected = await self._turn_settings(context)
        env, gh_config_dir = isolated_agent_environment(context.cwd)
        env.update(member_command_environment(context))
        env.update(delegation_environment(context.run_id))
        log_fd, log_path = tempfile.mkstemp(prefix="guildbotics-agy-log-")
        os.close(log_fd)
        log_file = Path(log_path)
        args = [
            self._executable,
            "--print",
            prompt,
            "--output-format",
            "stream-json",
            _WORKSPACE_FLAG,
            str(context.cwd),
            "--dangerously-skip-permissions",
            "--print-timeout",
            f"{int(self._timeout)}s",
            "--log-file",
            str(log_file),
        ]
        if conversation.provider_session_id:
            args.extend(("--conversation", conversation.provider_session_id))
        if model := str(settings.get("model", "")):
            args.extend(("--model", model))
        elif effort := str(settings.get("effort", "")):
            args.extend(("--effort", effort))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(context.cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_READ_LIMIT,
            )
        except OSError as exc:
            remove_isolated_config(gh_config_dir)
            log_file.unlink(missing_ok=True)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"Could not start Antigravity: {exc}",
            ) from exc
        process = self._process
        assert process.stdout is not None
        stderr_task = (
            asyncio.create_task(process.stderr.read())
            if process.stderr is not None
            else None
        )
        events: list[AgentEvent] = []
        conversation_id = conversation.provider_session_id
        terminal_output = ""
        usage: dict[str, int] = {}
        terminal_seen = False
        terminal_error: AgentRuntimeError | None = None
        observed_returncode: int | None = None

        async def _publish(event: AgentEvent) -> None:
            events.append(event)
            emitted = emit(event)
            if asyncio.iscoroutine(emitted):
                await emitted

        for event in _start_events(context, settings, rejected):
            await _publish(event)
        try:
            async with asyncio.timeout(self._timeout + _TIMEOUT_GRACE_SECONDS):
                while line := await process.stdout.readline():
                    try:
                        raw = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise AgentRuntimeError(
                            AgentRuntimeErrorCategory.PROTOCOL,
                            f"Malformed Antigravity stream-json event: {exc}",
                            rotate_session=True,
                        ) from exc
                    if not isinstance(raw, dict):
                        continue
                    conversation_id = _conversation_id_of(raw) or conversation_id
                    for decoded in _decode_events(raw, conversation_id):
                        await _publish(decoded)
                    if raw.get("event") == "result":
                        result = _dict(raw.get("result"))
                        terminal_seen = True
                        terminal_output = str(result.get("response", "") or "").strip()
                        usage = _usage(result.get("usage"))
                        terminal_error = _result_error(result)
                        break
        except TimeoutError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Antigravity turn timed out.",
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
                f"Antigravity stream-json output could not be read: {exc}",
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
            log_tail = _read_log_tail(log_file)
            log_file.unlink(missing_ok=True)
            remove_isolated_config(gh_config_dir)
            self._process = None
        if terminal_error is not None:
            terminal_error.details["log_tail"] = log_tail
            terminal_error.details["stderr"] = stderr.strip()
            raise terminal_error
        # A non-error terminal result is authoritative. Any later negative exit
        # status can be caused by our cleanup of a CLI that is still waiting for
        # background descendants, and must not discard the valid response or
        # rotate its resumable conversation.
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
                f"Antigravity exited with code {returncode}: "
                f"{stderr.strip() or 'no output'}",
                details={"returncode": returncode, "log_tail": log_tail},
                rotate_session=True,
            )
        if not terminal_seen:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Antigravity stream ended without a terminal result event.",
                details={"log_tail": log_tail},
                rotate_session=True,
            )
        if not conversation_id:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Antigravity returned no conversation id.",
                details={"log_tail": log_tail},
                rotate_session=True,
            )
        if not terminal_output:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Antigravity completed without a terminal response.",
                details={"log_tail": log_tail, "stderr": stderr.strip()},
                rotate_session=True,
            )
        return AgentTerminalResult(
            output=terminal_output,
            events=tuple(events),
            provider_session_id=conversation_id,
            finish_reason="completed",
            usage=usage,
            stderr=stderr.strip(),
            returncode=returncode,
        )

    async def interrupt(self) -> None:
        if self._process is not None and self._process.returncode is None:
            await terminate_process_tree(self._process)

    async def close(self) -> None:
        await self.interrupt()

    async def _turn_settings(
        self, context: AgentExecutionContext
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Resolve the turn's effort mapping into the flags ``agy`` accepts.

        Returns the settings to send and, for the settings event, the ones that
        were dropped with the reason. ``--model`` wins over ``--effort`` because
        it is the more specific request and ``agy`` refuses both together.
        """
        requested = _requested_settings(context)
        rejected: dict[str, str] = {}
        unknown = sorted(set(context.provider_options) - _EFFORT_SETTING_KEYS)
        if unknown:
            _LOGGER.warning(
                "Ignoring unsupported Antigravity effort settings: %s",
                ", ".join(unknown),
            )
        model = str(requested.get("model", ""))
        if model and (catalog := await self._models()) and model not in catalog:
            _LOGGER.warning(
                "Antigravity does not offer model '%s'; ignoring it.", model
            )
            rejected["model"] = "not offered by `agy models`"
            requested.pop("model")
            model = ""
        if model and "effort" in requested:
            # Not a guess about the id's shape: `agy` rejects the pair for every
            # catalogued model, either because the tier is part of the id or
            # because the model takes no effort at all.
            rejected["effort"] = "not combinable with an explicit model"
            requested.pop("effort")
        return requested, rejected

    async def _models(self) -> frozenset[str]:
        """The model ids ``agy models`` offers, empty when it cannot be read."""
        if self._model_catalog_read:
            return self._model_catalog
        self._model_catalog_read = True
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                "models",
                # `agy models` blocks forever on an attached terminal.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=_MODELS_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            if process is not None and process.returncode is None:
                await terminate_process_tree(process)
            # Validation is a convenience; a catalog we cannot read must not
            # stop the turn.
            _LOGGER.warning("Could not read the Antigravity model catalog: %s", exc)
            return self._model_catalog
        if process.returncode != 0:
            return self._model_catalog
        self._model_catalog = frozenset(
            identifier
            for line in stdout.decode(errors="replace").splitlines()
            if (identifier := line.strip())
        )
        return self._model_catalog

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
                # `agy` subcommands block forever on an attached terminal.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_HELP_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            if process is not None and process.returncode is None:
                await terminate_process_tree(process)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                f"Could not inspect Antigravity stream-json capabilities: {exc}",
            ) from exc
        finally:
            remove_isolated_config(gh_config_dir)
        # `agy --help` prints to stderr and exits 0, so both pipes are read.
        help_text = (stdout + stderr).decode(errors="replace")
        required = (
            "--print",
            "--output-format",
            "--conversation",
            "--model",
            "--effort",
            _WORKSPACE_FLAG,
        )
        missing = [flag for flag in required if flag not in help_text]
        if process.returncode != 0 or missing:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                "The installed Antigravity version does not expose the required "
                "stream-json and exact-resume capabilities.",
                details={"missing_capabilities": missing},
            )
        self._capabilities_checked = True


def _requested_settings(context: AgentExecutionContext) -> dict[str, Any]:
    """The effort-mapping values this adapter recognizes, normalized."""
    settings: dict[str, Any] = {}
    if model := str(context.provider_options.get("model", "") or "").strip():
        settings["model"] = model
    effort = str(context.provider_options.get("effort", "") or "").strip().lower()
    if effort in _EFFORT_VALUES:
        settings["effort"] = effort
    elif effort:
        _LOGGER.warning("Ignoring unsupported Antigravity effort '%s'.", effort)
    return settings


def _start_events(
    context: AgentExecutionContext,
    settings: dict[str, Any],
    rejected: dict[str, str],
) -> list[AgentEvent]:
    """The policy and settings this turn runs under, recorded before it starts."""
    return [
        AgentEvent(
            AgentEventKind.APPROVAL,
            "policy",
            approval="always-proceed",
            details={
                "read_only": context.read_only,
                # `agy` offers no provider-side way to hold a turn to reads:
                # `--mode plan` still writes under `--dangerously-skip-permissions`,
                # `--sandbox` only confines terminal commands (its own file tools
                # escape the workspace), and dropping the permission skip makes
                # headless mode auto-deny every command and return nothing.
                "read_only_enforced": False,
            },
        ),
        AgentEvent(
            AgentEventKind.PROCESS,
            "settings",
            details={
                # What was asked for, never a guess at what the account default
                # resolved to: `agy` only reports a model back when one was
                # named on the command line.
                "model": str(settings.get("model", "")),
                "effort": str(settings.get("effort", "")),
                "rejected": rejected,
            },
        ),
    ]


def _conversation_id_of(raw: dict[str, Any]) -> str:
    """The conversation id an envelope carries, wherever ``agy`` puts it."""
    payload = _dict(raw.get(str(raw.get("event", ""))))
    return str(raw.get("conversation_id") or payload.get("conversation_id") or "")


def _decode_events(raw: dict[str, Any], conversation_id: str) -> list[AgentEvent]:
    event_type = str(raw.get("event", ""))
    if event_type == "init":
        return _init_events(_dict(raw.get("init")), conversation_id)
    if event_type == "step_update":
        return _step_events(_dict(raw.get("step_update")), conversation_id)
    if event_type == "result":
        return _result_events(_dict(raw.get("result")), conversation_id)
    return []


def _init_events(init: dict[str, Any], conversation_id: str) -> list[AgentEvent]:
    return [
        AgentEvent(
            AgentEventKind.PROCESS,
            "initialized",
            provider_session_id=conversation_id,
            details={
                "model": str(init.get("model", "") or ""),
                "cwd": str(init.get("cwd", "") or ""),
                "permission_mode": str(init.get("permission_mode", "") or ""),
                "tools": init.get("tools", []),
            },
        )
    ]


def _step_events(step: dict[str, Any], conversation_id: str) -> list[AgentEvent]:
    step_type = str(step.get("step_type", ""))
    state = str(step.get("state", ""))
    if step_type == "agent_response":
        # Both the ACTIVE and the DONE update carry a fragment, never the whole
        # reply, so every one of them is a delta. The complete text arrives with
        # the terminal result.
        text = str(step.get("text_delta", "") or "")
        if not text:
            return []
        return [
            AgentEvent(
                AgentEventKind.ASSISTANT,
                "delta",
                message=text,
                provider_session_id=conversation_id,
            )
        ]
    if step_type == "tool":
        return _tool_events(step, state, conversation_id)
    if step_type == "error_message":
        return [
            AgentEvent(
                AgentEventKind.PROCESS,
                "error_message",
                provider_session_id=conversation_id,
                details={"step_index": step.get("step_index")},
            )
        ]
    return []


def _tool_events(
    step: dict[str, Any], state: str, conversation_id: str
) -> list[AgentEvent]:
    info = _dict(step.get("tool_info"))
    parameters = _dict(info.get("parameters"))
    tool_name = str(step.get("tool_name") or info.get("name") or "")
    if tool_name in _FILE_CHANGE_TOOLS:
        kind = AgentEventKind.FILE_CHANGE
    elif tool_name in _COMMAND_TOOLS:
        kind = AgentEventKind.COMMAND
    else:
        kind = AgentEventKind.TOOL
    command = str(parameters.get("CommandLine", "") or tool_name)
    path = next(
        (
            value
            for key in _PATH_PARAMETERS
            if isinstance(value := parameters.get(key), str) and value
        ),
        "",
    )
    if state == "ACTIVE":
        return [
            AgentEvent(
                kind,
                "started",
                provider_session_id=conversation_id,
                item_id=_step_id(step),
                command=command,
                path=path,
                details={"tool": tool_name},
            )
        ]
    error = _dict(info.get("error"))
    return [
        AgentEvent(
            kind,
            "completed",
            message=str(error.get("message") or info.get("output") or ""),
            provider_session_id=conversation_id,
            item_id=_step_id(step),
            command=command,
            path=path,
            details={"tool": tool_name, "is_error": state == "ERROR"},
        )
    ]


def _result_events(result: dict[str, Any], conversation_id: str) -> list[AgentEvent]:
    usage = _usage(result.get("usage"))
    events = [
        AgentEvent(
            AgentEventKind.TURN,
            "completed",
            message=str(result.get("response", "") or ""),
            provider_session_id=conversation_id,
            usage=usage,
            details={
                "status": str(result.get("status", "") or ""),
                "num_turns": result.get("num_turns"),
                "duration_seconds": result.get("duration_seconds"),
            },
        )
    ]
    if usage:
        events.append(
            AgentEvent(
                AgentEventKind.USAGE,
                "turn",
                provider_session_id=conversation_id,
                usage=usage,
            )
        )
    return events


def _result_error(result: dict[str, Any]) -> AgentRuntimeError | None:
    """Classify a terminal result ``agy`` did not report as successful."""
    status = str(result.get("status", "") or "")
    if status == "SUCCESS":
        return None
    message = str(result.get("error", "") or "").strip()
    details: dict[str, Any] = {"status": status}
    if _RATE_LIMIT_PATTERN.search(message):
        if retry_after := _RETRY_AFTER_PATTERN.search(message):
            details["retry_after_text"] = retry_after.group(0)
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.RATE_LIMITED,
            message or "Antigravity quota is exhausted.",
            details=details,
            rotate_session=False,
        )
    if _AUTHENTICATION_PATTERN.search(message):
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            message or "Antigravity authentication failed.",
            details=details,
            rotate_session=True,
        )
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.PROCESS,
        message or f"Antigravity returned a '{status}' result.",
        details=details,
        rotate_session=True,
    )


def _usage(value: Any) -> dict[str, int]:
    """Normalize ``agy``'s token counts onto the keys shared by all adapters."""
    raw = _dict(value)
    output: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_tokens", "cached_input_tokens"),
        ("thinking_tokens", "reasoning_output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        if source not in raw:
            continue
        try:
            output[target] = max(0, int(raw[source]))
        except (TypeError, ValueError):
            continue
    return output


def _step_id(step: dict[str, Any]) -> str:
    index = step.get("step_index")
    return f"step-{index}" if index is not None else ""


def _read_log_tail(path: Path) -> str:
    """The end of ``agy``'s own log, for a failure that has nothing else."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-_LOG_TAIL_BYTES:].decode(errors="replace").strip()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
