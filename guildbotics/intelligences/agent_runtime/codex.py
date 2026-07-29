"""Codex App Server JSONL adapter."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    isolated_agent_environment,
    member_command_environment,
    remove_isolated_config,
    terminate_process_tree,
)
from guildbotics.intelligences.agent_runtime.jsonrpc import (
    FATAL_NOTIFICATION,
    METHOD_NOT_FOUND,
    LineJsonRpcTransport,
    RpcError,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationRecord,
    EventSink,
)
from guildbotics.intelligences.agent_runtime.policy import AdapterFilesystemPolicy
from guildbotics.intelligences.agent_runtime.usage import parse_codex_rate_limits
from guildbotics.runtime.person_lease import delegation_environment

_MODERN_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)
_LEGACY_APPROVAL_METHODS = frozenset({"execCommandApproval", "applyPatchApproval"})
_UNSUPPORTED_APPROVAL_METHODS = frozenset({"item/permissions/requestApproval"})
_APPROVAL_POLICY = "never"


class CodexAppServerAdapter:
    name = "codex-app-server"

    def __init__(
        self,
        *,
        executable: str = "codex",
        timeout: float = 3600.0,
        policy: AdapterFilesystemPolicy | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._transport = LineJsonRpcTransport(
            label="Codex App Server",
            request_timeout=min(timeout, 30.0),
            on_reverse_request=self._handle_server_request,
        )
        self._gh_config_dir = ""
        self._active_thread_id = ""
        self._active_turn_id = ""
        self._policy = policy or AdapterFilesystemPolicy()

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        await self._ensure_started(context, emit)
        await self._check_account()
        await self._check_rate_limits()
        policy_event = AgentEvent(
            AgentEventKind.APPROVAL,
            "policy",
            approval=_APPROVAL_POLICY,
            details={"filesystem_access": self._policy.filesystem_access},
        )
        emitted = emit(policy_event)
        if asyncio.iscoroutine(emitted):
            await emitted
        thread_id = await self._resolve_thread(context, conversation)
        self._active_thread_id = thread_id
        try:
            response = await self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": str(context.cwd),
                    "approvalPolicy": _APPROVAL_POLICY,
                    "sandboxPolicy": _sandbox_policy(
                        self._policy,
                        str(context.workspace_data_root),
                        context.read_only,
                    ),
                },
            )
        except RpcError as exc:
            raise _agent_error_from_rpc(exc) from exc
        turn = _dict(_dict(response).get("turn"))
        self._active_turn_id = _identifier(turn)
        events: list[AgentEvent] = []
        assistant_deltas: list[str] = []
        assistant_final = ""
        usage: dict[str, int] = {}
        finish_reason = "completed"

        async def publish(event: AgentEvent) -> None:
            events.append(event)
            result = emit(event)
            if asyncio.iscoroutine(result):
                await result

        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    message = await self._transport.next_notification()
                    method = str(message.get("method", ""))
                    if method == FATAL_NOTIFICATION:
                        raise self._transport.fatal_error or AgentRuntimeError(
                            AgentRuntimeErrorCategory.PROCESS,
                            "Codex App Server stopped unexpectedly.",
                            rotate_session=True,
                        )
                    params = _dict(message.get("params"))
                    if not _belongs_to_turn(params, thread_id, self._active_turn_id):
                        continue
                    event = _decode_notification(method, params)
                    if event is not None:
                        await publish(event)
                        if event.kind is AgentEventKind.ASSISTANT and event.message:
                            if event.name == "delta":
                                assistant_deltas.append(event.message)
                            elif event.name == "completed":
                                assistant_final = event.message
                        if event.usage:
                            usage.update(event.usage)
                    notification_error = _error_notification(method, params)
                    if notification_error is not None:
                        if (
                            notification_error.category
                            is AgentRuntimeErrorCategory.RATE_LIMITED
                        ):
                            try:
                                await self._check_rate_limits()
                            except AgentRuntimeError as refreshed:
                                raise refreshed from notification_error
                        raise notification_error
                    if method == "turn/completed":
                        completed_turn = _dict(params.get("turn"))
                        finish_reason = str(
                            completed_turn.get("status")
                            or params.get("status")
                            or "completed"
                        )
                        terminal_error = _turn_error(completed_turn.get("error"))
                        if terminal_error is not None:
                            raise terminal_error
                        break
        except TimeoutError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Codex turn timed out.",
                rotate_session=True,
            ) from exc
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        finally:
            self._active_turn_id = ""

        output = ("".join(assistant_deltas) or assistant_final).strip()
        if not output:
            output = _terminal_output(events)
        if finish_reason not in {"completed", "success"}:
            category = (
                AgentRuntimeErrorCategory.CANCELLED
                if finish_reason in {"interrupted", "cancelled"}
                else AgentRuntimeErrorCategory.PROCESS
            )
            raise AgentRuntimeError(
                category,
                f"Codex turn finished with status '{finish_reason}'.",
                rotate_session=True,
            )
        if not output:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Codex turn completed without a terminal response.",
                rotate_session=True,
            )
        return AgentTerminalResult(
            output=output,
            events=tuple(events),
            provider_session_id=thread_id,
            provider_turn_id=_identifier(turn) or self._active_turn_id,
            finish_reason=finish_reason,
            usage=usage,
            stderr=self._transport.stderr_text(),
        )

    async def interrupt(self) -> None:
        if self._active_thread_id and self._active_turn_id:
            # A second cancellation while the interrupt RPC is pending must not
            # skip the process-tree termination below.
            with suppress(asyncio.CancelledError, Exception):
                await self._request(
                    "turn/interrupt",
                    {
                        "threadId": self._active_thread_id,
                        "turnId": self._active_turn_id,
                    },
                )
        process = self._transport.process
        if process is not None and process.returncode is None:
            await terminate_process_tree(process)

    async def close(self) -> None:
        process = self._transport.process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with suppress(BrokenPipeError, ConnectionError, OSError):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                await terminate_process_tree(process)
        await self._transport.aclose()
        remove_isolated_config(self._gh_config_dir)

    async def _ensure_started(
        self, context: AgentExecutionContext, emit: EventSink
    ) -> None:
        if self._transport.running:
            return
        if self._transport.process is not None:
            await self.close()
        cwd = context.cwd
        env, self._gh_config_dir = isolated_agent_environment(cwd)
        env.update(member_command_environment(context))
        env.update(delegation_environment(context.run_id))
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                "app-server",
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_READ_LIMIT,
            )
        except OSError as exc:
            remove_isolated_config(self._gh_config_dir)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"Could not start Codex App Server: {exc}",
            ) from exc
        self._transport.start(process)
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "guildbotics",
                        "title": "GuildBotics",
                        "version": "1",
                    }
                },
            )
            await self._notify("initialized", {})
        except RpcError as exc:
            await self.close()
            raise _agent_error_from_rpc(exc) from exc
        except Exception:
            await self.close()
            raise
        event = AgentEvent(AgentEventKind.PROCESS, "started")
        result = emit(event)
        if asyncio.iscoroutine(result):
            await result

    async def _resolve_thread(
        self, context: AgentExecutionContext, conversation: ConversationRecord
    ) -> str:
        if conversation.provider_session_id:
            try:
                response = await self._request(
                    "thread/resume", {"threadId": conversation.provider_session_id}
                )
            except RpcError as exc:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.SESSION_UNAVAILABLE,
                    "The exact Codex thread could not be resumed.",
                    details={"provider_error": str(exc)},
                    rotate_session=True,
                ) from exc
        else:
            response = await self._request(
                "thread/start",
                {
                    "cwd": str(context.cwd),
                    "approvalPolicy": _APPROVAL_POLICY,
                    "sandbox": _thread_sandbox(self._policy, context.read_only),
                },
            )
        thread_id = _identifier(_dict(_dict(response).get("thread")))
        if not thread_id:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Codex returned no thread id.",
                rotate_session=True,
            )
        return thread_id

    async def _check_account(self) -> None:
        try:
            result = _dict(await self._request("account/read", {"refreshToken": False}))
        except RpcError as exc:
            raise _agent_error_from_rpc(exc) from exc
        requires_auth = result.get(
            "requiresOpenaiAuth", result.get("requires_openai_auth")
        )
        if bool(requires_auth) and not result.get("account"):
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "Codex login is required.",
            )

    async def _check_rate_limits(self) -> None:
        try:
            result = _dict(await self._request("account/rateLimits/read", {}))
        except RpcError:
            # API-key and non-ChatGPT providers may not expose this capability.
            return
        snapshot = parse_codex_rate_limits(result)
        if snapshot.limit_reached:
            details: dict[str, Any] = {}
            resets = [
                window.resets_at for window in snapshot.windows if window.resets_at
            ]
            if resets:
                details["retry_after_at"] = min(resets)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.RATE_LIMITED,
                "Codex account rate limit is active.",
                details=details,
            )

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        return await self._transport.request(method, params)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._transport.notify(method, params)

    async def _handle_server_request(
        self, method: str, request_id: Any, _params: dict[str, Any]
    ) -> None:
        if method in _MODERN_APPROVAL_METHODS | _LEGACY_APPROVAL_METHODS:
            decision = "decline" if method in _MODERN_APPROVAL_METHODS else "denied"
            await self._transport.respond(request_id, result={"decision": decision})
            self._transport.push_notification(
                {
                    "method": "guildbotics/approval",
                    "params": {
                        "approval": "decline",
                        "requestMethod": method,
                    },
                }
            )
            return
        if method in _UNSUPPORTED_APPROVAL_METHODS:
            await self._transport.respond(
                request_id,
                error={
                    "code": METHOD_NOT_FOUND,
                    "message": f"Unsupported approval request: {method}",
                },
            )
            self._transport.push_notification(
                {
                    "method": "guildbotics/approval",
                    "params": {
                        "approval": "decline",
                        "requestMethod": method,
                        "unsupported": True,
                    },
                }
            )
            return
        await self._transport.respond(
            request_id,
            error={
                "code": METHOD_NOT_FOUND,
                "message": f"Unsupported request: {method}",
            },
        )


def _thread_sandbox(policy: AdapterFilesystemPolicy, read_only: bool = False) -> str:
    if read_only:
        return "read-only"
    return (
        "danger-full-access"
        if policy.filesystem_access == "host"
        else "workspace-write"
    )


def _sandbox_policy(
    policy: AdapterFilesystemPolicy, workspace_data_root: str, read_only: bool = False
) -> dict[str, Any]:
    # A read-only turn wins over the configured filesystem access: it exists to
    # inspect recorded state, and what it reads is untrusted input.
    if read_only:
        return {"type": "readOnly", "networkAccess": False}
    if policy.filesystem_access == "host":
        return {"type": "dangerFullAccess"}
    return {
        "type": "workspaceWrite",
        "writableRoots": [workspace_data_root],
        "networkAccess": True,
    }


def _decode_notification(method: str, params: dict[str, Any]) -> AgentEvent | None:
    session_id = _identifier(_dict(params.get("thread"))) or str(
        params.get("threadId", params.get("thread_id", "")) or ""
    )
    turn_id = _identifier(_dict(params.get("turn"))) or str(
        params.get("turnId", params.get("turn_id", "")) or ""
    )
    item = _dict(params.get("item"))
    item_id = _identifier(item)
    if method == "thread/compacted" or (
        method == "item/completed" and item.get("type") == "contextCompaction"
    ):
        return AgentEvent(
            AgentEventKind.TURN,
            "context_compaction",
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            item_id=item_id,
            details={"provider_event": method},
        )
    if method == "item/agentMessage/delta":
        return AgentEvent(
            AgentEventKind.ASSISTANT,
            "delta",
            message=str(params.get("delta", "") or ""),
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            item_id=item_id,
        )
    if method in {"item/started", "item/completed"}:
        item_type = str(item.get("type", "") or "unknown")
        # Reasoning items never carry text and user-message items (injected
        # context) only carry content once completed; skip the empty shells so
        # transcripts stay signal.
        if item_type == "reasoning" or (
            item_type == "userMessage" and method == "item/started"
        ):
            return None
        kind = {
            "agentMessage": AgentEventKind.ASSISTANT,
            "commandExecution": AgentEventKind.COMMAND,
            "fileChange": AgentEventKind.FILE_CHANGE,
        }.get(item_type, AgentEventKind.TOOL)
        message = _item_text(item) if method == "item/completed" else ""
        paths = _item_paths(item)
        details: dict[str, Any] = {
            "item_type": item_type,
            "status": item.get("status"),
        }
        if paths:
            details["paths"] = paths
        return AgentEvent(
            kind,
            method.removeprefix("item/"),
            message=message,
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            item_id=item_id,
            command=_item_command(item),
            path=paths[0] if paths else "",
            details=details,
        )
    if method == "thread/tokenUsage/updated":
        token_usage = _dict(params.get("tokenUsage", params.get("token_usage")))
        latest = _dict(token_usage.get("last")) or _dict(token_usage.get("total"))
        return AgentEvent(
            AgentEventKind.USAGE,
            "updated",
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            usage=_usage(latest),
            details={
                "model_context_window": token_usage.get(
                    "modelContextWindow", token_usage.get("model_context_window")
                )
            },
        )
    if method == "turn/completed":
        turn = _dict(params.get("turn"))
        usage = _usage(_dict(turn.get("usage")) or _dict(params.get("usage")))
        return AgentEvent(
            AgentEventKind.TURN,
            "completed",
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            usage=usage,
            details={"status": turn.get("status") or params.get("status")},
        )
    if method == "guildbotics/approval":
        return AgentEvent(
            AgentEventKind.APPROVAL,
            "decision",
            approval=str(params.get("approval", "")),
            details={
                "request_method": params.get("requestMethod"),
                "unsupported": bool(params.get("unsupported", False)),
            },
        )
    if method == "error":
        error = _dict(params.get("error"))
        return AgentEvent(
            AgentEventKind.FAILED,
            "provider",
            message=str(error.get("message", "") or ""),
            provider_session_id=session_id,
            provider_turn_id=turn_id,
            details={
                "code": error.get("codexErrorInfo", error.get("codex_error_info")),
                "will_retry": bool(params.get("willRetry", params.get("will_retry"))),
            },
        )
    if method.startswith("turn/"):
        return AgentEvent(
            AgentEventKind.TURN,
            method.removeprefix("turn/"),
            provider_session_id=session_id,
            provider_turn_id=turn_id,
        )
    return None


def _belongs_to_turn(params: dict[str, Any], thread_id: str, turn_id: str) -> bool:
    event_thread = _identifier(_dict(params.get("thread"))) or str(
        params.get("threadId", params.get("thread_id", "")) or ""
    )
    event_turn = _identifier(_dict(params.get("turn"))) or str(
        params.get("turnId", params.get("turn_id", "")) or ""
    )
    return (not event_thread or event_thread == thread_id) and (
        not event_turn or not turn_id or event_turn == turn_id
    )


def _terminal_output(events: list[AgentEvent]) -> str:
    for event in reversed(events):
        if event.kind is AgentEventKind.ASSISTANT and event.message:
            return event.message.strip()
    return ""


def _item_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "message", "aggregatedOutput"):
        value = item.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [
                str(part.get("text", "")) for part in value if isinstance(part, dict)
            ]
            if any(parts):
                return "".join(parts)
    return ""


def _item_command(item: dict[str, Any]) -> str:
    command = item.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return ""


def _item_paths(item: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    direct = item.get("path")
    if isinstance(direct, str) and direct:
        paths.append(direct)
    changes = item.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    aliases = {
        "input_tokens": ("inputTokens", "input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
        "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
        "reasoning_output_tokens": (
            "reasoningOutputTokens",
            "reasoning_output_tokens",
        ),
        "total_tokens": ("totalTokens", "total_tokens"),
    }
    for target, keys in aliases.items():
        for key in keys:
            try:
                output[target] = int(raw[key])
                break
            except (KeyError, TypeError, ValueError):
                continue
    return output


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identifier(value: dict[str, Any]) -> str:
    return str(
        value.get("id")
        or value.get("threadId")
        or value.get("thread_id")
        or value.get("turnId")
        or value.get("turn_id")
        or ""
    )


def _agent_error_from_rpc(exc: RpcError) -> AgentRuntimeError:
    error = _dict(exc.error)
    data = _dict(error.get("data"))
    identifiers = {
        str(value).lower()
        for value in (
            error.get("type"),
            error.get("category"),
            data.get("type"),
            data.get("category"),
            data.get("code"),
        )
        if value is not None
    }
    details: dict[str, Any] = {
        "provider_code": error.get("code"),
        "provider_type": data.get("type") or error.get("type"),
    }
    for source, target in (
        ("retryAfterAt", "retry_after_at"),
        ("retry_after_at", "retry_after_at"),
        ("retryAfterSeconds", "retry_after_seconds"),
        ("retry_after_seconds", "retry_after_seconds"),
    ):
        if source in data:
            details[target] = data[source]
    if identifiers & {
        "authentication",
        "authentication_failed",
        "unauthorized",
        "not_authenticated",
    }:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            "Codex authentication failed.",
            details=details,
        )
    if identifiers & {"rate_limit", "rate_limited", "too_many_requests"}:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.RATE_LIMITED,
            "Codex account rate limit is active.",
            details=details,
        )
    if error.get("code") == METHOD_NOT_FOUND:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
            "The installed Codex App Server does not support the required protocol.",
            details=details,
        )
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.PROTOCOL,
        "Codex App Server returned a protocol error.",
        details=details,
        rotate_session=True,
    )


def _error_notification(
    method: str, params: dict[str, Any]
) -> AgentRuntimeError | None:
    if method != "error":
        return None
    if bool(params.get("willRetry", params.get("will_retry", False))):
        return None
    return _turn_error(params.get("error")) or AgentRuntimeError(
        AgentRuntimeErrorCategory.PROCESS,
        "Codex reported a terminal provider error.",
        rotate_session=True,
    )


def _turn_error(value: Any) -> AgentRuntimeError | None:
    error = _dict(value)
    if not error:
        return None
    raw_code = error.get("codexErrorInfo", error.get("codex_error_info"))
    code = raw_code if isinstance(raw_code, str) else ""
    details = {"provider_code": code} if code else {}
    if code in {"unauthorized", "authentication", "authenticationFailed"}:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            "Codex authentication failed.",
            details=details,
            rotate_session=True,
        )
    if code in {"usageLimitExceeded", "rateLimited", "rate_limit"}:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.RATE_LIMITED,
            "Codex account rate limit is active.",
            details=details,
            rotate_session=True,
        )
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.PROCESS,
        str(error.get("message", "") or "Codex turn failed."),
        details=details,
        rotate_session=True,
    )
