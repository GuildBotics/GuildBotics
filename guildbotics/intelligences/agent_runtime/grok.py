"""Grok Build Agent Client Protocol (ACP) adapter.

Verified against Grok Build 0.2.114 (``grok agent stdio``): ACP protocol
version 1, ``loadSession: true`` with no ``sessionCapabilities.resume``, and
the ``cached_token`` / ``grok.com`` authentication methods. The adapter gates on
those advertised capabilities rather than on the version string, so a newer
Grok Build that still speaks ACP v1 keeps working.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from logging import getLogger
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
from guildbotics.intelligences.agent_runtime.policy import AdapterFilesystemPolicy
from guildbotics.runtime.person_lease import delegation_environment

ACP_PROTOCOL_VERSION = 1
#: Version of the GuildBotics client contract, matching the Codex adapter's.
CLIENT_VERSION = "1"
_CACHED_TOKEN_METHOD = "cached_token"
_API_KEY_METHOD = "xai.api_key"
#: Interactive sign-in must never be started from a headless run.
_INTERACTIVE_AUTH_METHODS = frozenset({"grok.com"})
_APPROVAL_POLICY = "never"

#: ``_x.ai/session_notification`` carries xAI's private session updates. The
#: kinds below change how GuildBotics treats the conversation, so they are
#: normalized instead of discarded with the rest of the extension traffic.
_COMPACTION_UPDATES = frozenset(
    {
        "auto_compact_started",
        "auto_compact_completed",
        "auto_compact_failed",
        "auto_compact_cancelled",
        "compaction_checkpoint",
    }
)
#: Grok Build 0.2.114 wraps its private session updates in both of these.
_EXTENSION_NOTIFICATIONS = frozenset(
    {"_x.ai/session_notification", "_x.ai/session/update"}
)
#: Every channel that carries a session update, standard or private. Replayed
#: history arrives on all of them.
_SESSION_UPDATES = _EXTENSION_NOTIFICATIONS | {"session/update"}
_APPROVAL_NOTIFICATION = "guildbotics/approval"
#: Private channels observed on 0.2.114 that only carry peer UI state. They are
#: counted so a change stays visible, but their payloads echo prompt text, the
#: workspace path and promotional copy, so no sample is retained.
_KNOWN_EXTENSION_NOISE = frozenset(
    {
        "_x.ai/announcements/update",
        "_x.ai/mcp/servers_updated",
        "_x.ai/mcp_initialized",
        "_x.ai/models/update",
        "_x.ai/queue/changed",
        "_x.ai/session/prompt_complete",
        "_x.ai/sessions/changed",
        "_x.ai/settings/update",
    }
)
#: Standard ACP updates GuildBotics deliberately ignores; they describe the
#: peer's own UI affordances rather than the turn.
_IGNORED_UPDATES = frozenset(
    {
        "available_commands_update",
        "current_mode_update",
        "config_option_update",
    }
)
_STOP_REASONS = {
    "cancelled": (AgentRuntimeErrorCategory.CANCELLED, "Grok cancelled the turn."),
    "max_tokens": (
        AgentRuntimeErrorCategory.PROCESS,
        "Grok stopped the turn at the token limit.",
    ),
    "max_turn_requests": (
        AgentRuntimeErrorCategory.PROCESS,
        "Grok stopped the turn at the request limit.",
    ),
    "refusal": (AgentRuntimeErrorCategory.PROCESS, "Grok refused the turn."),
}
_RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit",
        "rate_limited",
        "usage_pool_exhausted",
        "usage_limit_reached",
        "global_rate_limit",
        "too_many_requests",
    }
)
_AUTH_CODES = frozenset(
    {
        "unauthorized",
        "authentication",
        "authentication_failed",
        "not_authenticated",
    }
)
#: The launch options this adapter can set on `grok agent stdio`.
_EFFORT_SETTING_KEYS = frozenset({"model", "reasoning_effort"})
_LOGGER = getLogger(__name__)
_MAX_FIELDS = 20
_MAX_FIELD_NAME = 64
#: Tool kinds that actually change the workspace. ACP `locations` reports every
#: file a tool touched, including reads, so it must not drive this.
_FILE_CHANGE_KINDS = frozenset({"edit", "delete", "move"})
#: Preferred first: declining once must not memorize the decision.
_REJECT_KINDS = ("reject_once", "reject_always")


class GrokAcpAdapter:
    name = "grok-acp"
    # `grok agent stdio` takes the model and reasoning effort as launch options,
    # so they are fixed for the life of the process: changing them needs a fresh
    # session rather than a mid-conversation adjustment.
    settings_scope = SETTINGS_SCOPE_SESSION

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        return _applied_effort_settings(context)

    def __init__(
        self,
        *,
        executable: str = "grok",
        timeout: float = 3600.0,
        policy: AdapterFilesystemPolicy | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._transport = LineJsonRpcTransport(
            label="Grok Build ACP",
            include_version=True,
            request_timeout=min(timeout, 30.0),
            on_reverse_request=self._handle_agent_request,
        )
        self._policy = policy or AdapterFilesystemPolicy()
        self._gh_config_dir = ""
        self._capabilities: dict[str, Any] = {}
        self._agent_version = ""
        self._auth_method = ""
        self._active_session_id = ""
        self._unhandled: dict[str, dict[str, Any]] = {}
        self._tool_kinds: dict[str, str] = {}
        self._context_used = 0
        self._context_size = 0

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        await self._ensure_started(context, emit)
        _warn_unusable_effort_settings(context)
        self._unhandled = {}
        self._tool_kinds = {}
        self._context_used = 0
        self._context_size = 0
        await _publish(
            emit,
            AgentEvent(
                AgentEventKind.APPROVAL,
                "policy",
                approval=_APPROVAL_POLICY,
                details={
                    "filesystem_access": self._policy.filesystem_access,
                    "sandbox": _sandbox_profile(self._policy, context.read_only),
                },
            ),
        )
        session_id = await self._resolve_session(context, conversation, emit)
        self._active_session_id = session_id
        events: list[AgentEvent] = []
        chunks = _AssistantBuffer()
        usage: dict[str, int] = {}

        async def publish(event: AgentEvent) -> None:
            events.append(event)
            await _publish(emit, event)

        prompt_task = asyncio.create_task(
            self._transport.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                # ACP answers session/prompt only when the turn ends, so a
                # per-request deadline would cut real work short. The whole turn
                # is bounded by _consume_turn's timeout instead.
                timeout=None,
            )
        )
        try:
            stop_reason = await self._consume_turn(
                prompt_task, session_id, publish, chunks, usage
            )
            output = chunks.text().strip()
            if stop_reason == "end_turn" and output:
                # ACP has no terminal assistant update, but every adapter must
                # end a finished stream with the whole reply: the display layer
                # drops the per-chunk records once it arrives.
                await publish(
                    AgentEvent(
                        AgentEventKind.ASSISTANT,
                        "completed",
                        message=output,
                        provider_session_id=session_id,
                    )
                )
        except RpcError as exc:
            raise _agent_error_from_rpc(exc) from exc
        except TimeoutError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Grok turn timed out.",
                rotate_session=True,
            ) from exc
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        finally:
            prompt_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await prompt_task
            self._active_session_id = ""
            with suppress(Exception):
                await self._publish_extension_summary(emit, session_id)

        if stop_reason != "end_turn":
            raise _stop_reason_error(stop_reason)
        if not output:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Grok completed the turn without a terminal response.",
                rotate_session=True,
            )
        if self._context_size > 0:
            usage["context_used_tokens"] = self._context_used
            usage["context_size_tokens"] = self._context_size
        return AgentTerminalResult(
            output=output,
            events=tuple(events),
            provider_session_id=session_id,
            # ACP has no standard provider turn identifier; JSON-RPC request ids
            # are transport-local and must not be persisted as one.
            provider_turn_id="",
            finish_reason="completed",
            usage=usage,
            stderr=self._transport.stderr_text(),
        )

    async def interrupt(self) -> None:
        if self._active_session_id:
            with suppress(asyncio.CancelledError, Exception):
                await self._transport.notify(
                    "session/cancel", {"sessionId": self._active_session_id}
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
        argv = _launch_argv(
            self._executable,
            self._policy,
            context.read_only,
            _applied_effort_settings(context),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
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
                f"Could not start Grok Build: {exc}",
            ) from exc
        self._transport.start(process)
        try:
            await self._initialize(env)
        except AgentRuntimeError:
            await self.close()
            raise
        except RpcError as exc:
            await self.close()
            raise _agent_error_from_rpc(exc) from exc
        except Exception:
            await self.close()
            raise
        await _publish(
            emit,
            AgentEvent(
                AgentEventKind.PROCESS,
                "started",
                details={
                    "agent_version": self._agent_version,
                    "protocol_version": ACP_PROTOCOL_VERSION,
                    "load_session": bool(self._capabilities.get("loadSession")),
                    "resume_session": self._supports_resume,
                    "auth_method": self._auth_method,
                },
            ),
        )

    async def _initialize(self, env: dict[str, str]) -> None:
        result = _dict(
            await self._transport.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    # Only capabilities GuildBotics actually implements are
                    # declared; an undeclared capability would let Grok call
                    # back into a client service that does not exist.
                    "clientCapabilities": {},
                    # ACP requires clientInfo.version; Grok rejects the request
                    # with "missing field `version`" when it is absent.
                    "clientInfo": {
                        "name": "guildbotics",
                        "title": "GuildBotics",
                        "version": CLIENT_VERSION,
                    },
                },
            )
        )
        if int(result.get("protocolVersion", 0) or 0) != ACP_PROTOCOL_VERSION:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                "The installed Grok Build does not speak Agent Client Protocol v1.",
                details={"protocol_version": result.get("protocolVersion")},
            )
        self._capabilities = _dict(result.get("agentCapabilities"))
        self._agent_version = str(_dict(result.get("_meta")).get("agentVersion", ""))
        if not self._capabilities.get("loadSession") and not self._supports_resume:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                "The installed Grok Build cannot resume an exact session.",
                details={"agent_version": self._agent_version},
            )
        await self._authenticate(result, env)

    @property
    def _supports_resume(self) -> bool:
        # An advertised-but-disabled capability is a refusal, not an offer:
        # calling session/resume on it would only earn a protocol error.
        return bool(_dict(self._capabilities.get("sessionCapabilities")).get("resume"))

    async def _authenticate(self, result: dict[str, Any], env: dict[str, str]) -> None:
        methods = [
            str(_dict(method).get("id", ""))
            for method in result.get("authMethods", [])
            if isinstance(method, dict)
        ]
        chosen = ""
        if _CACHED_TOKEN_METHOD in methods:
            chosen = _CACHED_TOKEN_METHOD
        elif _API_KEY_METHOD in methods and env.get("XAI_API_KEY", "").strip():
            chosen = _API_KEY_METHOD
        if not chosen:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "Grok Build has no usable saved login. "
                "Run 'grok login' (or 'grok login --device-auth') as this user.",
                details={
                    "advertised_methods": [
                        method
                        for method in methods
                        if method not in _INTERACTIVE_AUTH_METHODS
                    ]
                },
            )
        try:
            await self._transport.request("authenticate", {"methodId": chosen})
        except RpcError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "Grok Build authentication failed. "
                "Run 'grok login' (or 'grok login --device-auth') as this user.",
                details={"auth_method": chosen, "provider_error": str(exc)},
            ) from exc
        # Only the method identifier is recorded; credential values and the
        # contents of the Grok auth store are never read.
        self._auth_method = chosen

    async def _resolve_session(
        self,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> str:
        if not conversation.provider_session_id:
            try:
                result = _dict(
                    await self._transport.request(
                        "session/new",
                        {"cwd": str(context.cwd), "mcpServers": []},
                    )
                )
            except RpcError as exc:
                raise _agent_error_from_rpc(exc) from exc
            session_id = str(result.get("sessionId", "") or "")
            if not session_id:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROTOCOL,
                    "Grok returned no session id.",
                    rotate_session=True,
                )
            return session_id
        session_id = conversation.provider_session_id
        method = "session/resume" if self._supports_resume else "session/load"
        try:
            await self._transport.request(
                method,
                {
                    "sessionId": session_id,
                    "cwd": str(context.cwd),
                    "mcpServers": [],
                },
            )
        except RpcError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.SESSION_UNAVAILABLE,
                "The exact Grok session could not be resumed.",
                details={"provider_error": str(exc), "session_method": method},
                rotate_session=True,
            ) from exc
        # ACP replays the whole transcript before answering session/load, so the
        # response is the boundary: everything already queued is history and
        # must not reach the current turn, chat, or the normal transcript.
        replayed = self._absorb_replay()
        if replayed:
            await _publish(
                emit,
                AgentEvent(
                    AgentEventKind.TURN,
                    "history_rehydrated",
                    provider_session_id=session_id,
                    details={"replayed_updates": replayed, "session_method": method},
                ),
            )
        return session_id

    async def _consume_turn(
        self,
        prompt_task: asyncio.Task[Any],
        session_id: str,
        publish: Any,
        chunks: _AssistantBuffer,
        usage: dict[str, int],
    ) -> str:
        async def apply(message: dict[str, Any]) -> None:
            method = str(message.get("method", ""))
            params = _dict(message.get("params"))
            if params.get("sessionId") not in (None, "", session_id):
                return
            for event in self._decode(method, params, session_id):
                await publish(event)
                # Only the answer stream builds the reply. Reasoning chunks are
                # recorded for transcripts but must never reach the output.
                if event.kind is AgentEventKind.ASSISTANT and event.name == "delta":
                    chunks.add(event.item_id, event.message)
                if event.usage:
                    usage.update(event.usage)

        next_message = asyncio.create_task(self._transport.next_notification())
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    await asyncio.wait(
                        {next_message, prompt_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if next_message.done():
                        message = next_message.result()
                        next_message = asyncio.create_task(
                            self._transport.next_notification()
                        )
                        if str(message.get("method", "")) == FATAL_NOTIFICATION:
                            if prompt_task.done():
                                break
                            raise self._transport.fatal_error or AgentRuntimeError(
                                AgentRuntimeErrorCategory.PROCESS,
                                "Grok Build stopped unexpectedly.",
                                rotate_session=True,
                            )
                        await apply(message)
                        continue
                    break
                # The prompt response is the turn boundary, but updates queued
                # just before it must still reach the transcript.
                for queued in self._transport.drain_notifications():
                    if str(queued.get("method", "")) != FATAL_NOTIFICATION:
                        await apply(queued)
        finally:
            next_message.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await next_message
        return _stop_reason(prompt_task.result())

    def _decode(
        self, method: str, params: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        if method == "session/update":
            return self._decode_update(_dict(params.get("update")), session_id)
        if method == _APPROVAL_NOTIFICATION:
            return [
                AgentEvent(
                    AgentEventKind.APPROVAL,
                    "decision",
                    approval="decline",
                    provider_session_id=session_id,
                    details={
                        "tool_call": params.get("toolCall", ""),
                        "outcome": params.get("outcome", ""),
                    },
                )
            ]
        if method in _EXTENSION_NOTIFICATIONS:
            return self._decode_extension(_dict(params.get("update")), session_id)
        if method.startswith("_"):
            # Private extension traffic (settings, announcements, queue, model
            # and MCP state pushes) is aggregated by name so a new channel stays
            # visible without flooding the transcript with peer UI state.
            self._note_unhandled(method, params)
            return []
        return []

    def _decode_update(
        self, update: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        kind = str(update.get("sessionUpdate", "") or "")
        if kind in _IGNORED_UPDATES:
            return []
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            name = "delta" if kind == "agent_message_chunk" else "thinking"
            return [
                AgentEvent(
                    AgentEventKind.ASSISTANT,
                    name,
                    message=_content_text(update.get("content")),
                    provider_session_id=session_id,
                    item_id=str(update.get("messageId", "") or ""),
                )
            ]
        if kind in {"tool_call", "tool_call_update"}:
            return [self._tool_event(update, session_id, kind)]
        if kind == "usage_update":
            return self._usage_events(update, session_id)
        if kind in {"user_message_chunk", "session_info_update"}:
            return []
        self._note_unhandled(f"session/update:{kind}", update)
        return [
            AgentEvent(
                AgentEventKind.TOOL,
                "unknown_update",
                provider_session_id=session_id,
                details={"session_update": kind or "missing"},
            )
        ]

    def _decode_extension(
        self, update: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        kind = str(update.get("sessionUpdate", "") or "")
        if kind in _COMPACTION_UPDATES:
            return [
                AgentEvent(
                    AgentEventKind.TURN,
                    "context_compaction",
                    provider_session_id=session_id,
                    details={"detected_by": kind},
                )
            ]
        if kind == "retry_state":
            return _retry_state_events(update, session_id)
        if kind == "turn_completed":
            # Grok Build 0.2.114 never emits the standard ACP usage_update; the
            # only token counts it reports arrive here.
            return _turn_usage_events(update, session_id)
        self._note_unhandled(f"extension:{kind}", update)
        return []

    def _tool_event(
        self, update: dict[str, Any], session_id: str, kind: str
    ) -> AgentEvent:
        # Only `toolCallId` is required on a tool_call_update, so the kind
        # declared when the call started is what classifies later updates.
        call_id = str(update.get("toolCallId", "") or "")
        tool_kind = str(update.get("kind", "") or "")
        if tool_kind and call_id:
            self._tool_kinds[call_id] = tool_kind
        elif not tool_kind:
            tool_kind = self._tool_kinds.get(call_id, "")
        locations = [
            str(_dict(location).get("path", ""))
            for location in update.get("locations", [])
            if isinstance(location, dict) and _dict(location).get("path")
        ]
        details: dict[str, Any] = {
            "tool_kind": tool_kind or "unknown",
            "status": update.get("status"),
        }
        if locations:
            # These are the files the tool touched, which includes files it only
            # read; they describe the call, they do not classify it.
            details["paths"] = locations
        return AgentEvent(
            _event_kind_for_tool(tool_kind),
            "started" if kind == "tool_call" else "updated",
            message=str(update.get("title", "") or ""),
            provider_session_id=session_id,
            item_id=call_id,
            command=_tool_command(update),
            path=locations[0] if locations else "",
            details=details,
        )

    def _usage_events(
        self, update: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        used = _positive_int(update.get("used"))
        size = _positive_int(update.get("size"))
        details: dict[str, Any] = {}
        if used is None or size is None:
            self._note_unhandled("usage_update:malformed", update)
            return []
        compaction = self._context_size > 0 and used < self._context_used
        self._context_used = used
        self._context_size = size
        cost = update.get("cost")
        if isinstance(cost, dict):
            # Cost is a currency amount, not a token count; keeping it out of
            # usage stops it from being summed with tokens anywhere downstream.
            details["cost"] = {
                "amount": cost.get("amount"),
                "currency": cost.get("currency"),
            }
        events = [
            AgentEvent(
                AgentEventKind.USAGE,
                "context",
                provider_session_id=session_id,
                usage={"context_used_tokens": used, "context_size_tokens": size},
                details=details,
            )
        ]
        if compaction:
            # A drop in absolute session context means history was compacted.
            # This works even if Grok renames or drops its own notification.
            events.append(
                AgentEvent(
                    AgentEventKind.TURN,
                    "context_compaction",
                    provider_session_id=session_id,
                    details={"detected_by": "usage_decrease"},
                )
            )
        return events

    def _absorb_replay(self) -> int:
        """Consume queued history, keeping only the restored context snapshot.

        Returns the number of replayed session updates, so the replay stays
        diagnosable by count without copying any of it into the transcript.
        """
        replayed = 0
        for message in self._transport.drain_notifications():
            method = str(message.get("method", ""))
            if method not in _SESSION_UPDATES:
                if method.startswith("_"):
                    self._note_unhandled(method, _dict(message.get("params")))
                continue
            # Replayed session updates are history on both the standard and the
            # extension channels. They are counted, never decoded: a previous
            # turn's usage must not be reported as this turn's.
            replayed += 1
            update = _dict(_dict(message.get("params")).get("update"))
            if str(update.get("sessionUpdate", "")) != "usage_update":
                continue
            used = _positive_int(update.get("used"))
            size = _positive_int(update.get("size"))
            if used is not None and size is not None:
                self._context_used = used
                self._context_size = size
        return replayed

    def _note_unhandled(self, key: str, payload: dict[str, Any]) -> None:
        """Record that an unhandled channel appeared, without its content.

        Only the channel name, a count, and the payload's top-level field names
        are kept. Values are never stored: the diagnostics redactor works on
        mapping keys, so a serialized payload would carry any secret through
        verbatim.
        """
        entry = self._unhandled.setdefault(key, {"count": 0})
        entry["count"] = int(entry["count"]) + 1
        if key not in _KNOWN_EXTENSION_NOISE and "fields" not in entry:
            entry["fields"] = _payload_fields(payload)

    async def _publish_extension_summary(
        self, emit: EventSink, session_id: str
    ) -> None:
        if not self._unhandled:
            return
        await _publish(
            emit,
            AgentEvent(
                AgentEventKind.TURN,
                "protocol_extensions",
                provider_session_id=session_id,
                details={"unhandled": self._unhandled},
            ),
        )
        self._unhandled = {}

    async def _handle_agent_request(
        self, method: str, request_id: Any, params: dict[str, Any]
    ) -> None:
        if method == "session/request_permission":
            # The sandbox plus the startup approval policy is the boundary; an
            # unexpected request means Grok wants something outside it. The
            # option id is chosen by the agent per request, so it has to be read
            # back out of the request rather than assumed from the option kind.
            option_id = _reject_option_id(params.get("options"))
            outcome: dict[str, Any] = (
                {"outcome": "selected", "optionId": option_id}
                if option_id
                else {"outcome": "cancelled"}
            )
            await self._transport.respond(request_id, result={"outcome": outcome})
            self._transport.push_notification(
                {
                    "method": _APPROVAL_NOTIFICATION,
                    "params": {
                        "sessionId": self._active_session_id,
                        "toolCall": _dict(params.get("toolCall")).get("title", ""),
                        "outcome": outcome["outcome"],
                    },
                }
            )
            return
        # No client capability was declared, so any other reverse request is a
        # capability GuildBotics does not implement and must not fake.
        await self._transport.respond(
            request_id,
            error={
                "code": METHOD_NOT_FOUND,
                "message": f"Unsupported request: {method}",
            },
        )


class _AssistantBuffer:
    """Join assistant chunks in arrival order, with or without message ids."""

    def __init__(self) -> None:
        self._order: list[str] = []
        self._parts: dict[str, list[str]] = {}

    def add(self, message_id: str, text: str) -> None:
        if not text:
            return
        key = message_id or ""
        if key not in self._parts:
            self._parts[key] = []
            self._order.append(key)
        self._parts[key].append(text)

    def text(self) -> str:
        return "".join("".join(self._parts[key]) for key in self._order)


def _launch_argv(
    executable: str,
    policy: AdapterFilesystemPolicy,
    read_only: bool,
    settings: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    # `grok agent stdio` takes the model and reasoning effort as launch options.
    # They are process-wide rather than per-turn, which is exactly why this
    # adapter is session-scoped: changing them starts a fresh session.
    applied = settings or {}
    options: list[str] = []
    if model := str(applied.get("model", "") or ""):
        options.extend(("--model", model))
    if effort := str(applied.get("reasoning_effort", "") or ""):
        options.extend(("--reasoning-effort", effort))
    return (
        executable,
        # Headless runs must never let the CLI update itself mid-session.
        "--no-auto-update",
        "--sandbox",
        _sandbox_profile(policy, read_only),
        "--always-approve",
        *options,
        "agent",
        "stdio",
    )


def _applied_effort_settings(context: AgentExecutionContext) -> dict[str, Any]:
    """The effort settings this adapter can really impose, normalized.

    Silent by design: it also backs the session fingerprint, which is computed
    outside the run path and must not emit a second round of warnings.
    """
    return {
        key: value
        for key, value in context.provider_options.items()
        if key in _EFFORT_SETTING_KEYS and str(value or "")
    }


def _warn_unusable_effort_settings(context: AgentExecutionContext) -> None:
    """Report requested settings this adapter cannot act on."""
    unknown = sorted(set(context.provider_options) - _EFFORT_SETTING_KEYS)
    if unknown:
        _LOGGER.warning(
            "Ignoring unsupported Grok effort settings: %s", ", ".join(unknown)
        )


def _sandbox_profile(policy: AdapterFilesystemPolicy, read_only: bool = False) -> str:
    # A read-only turn only inspects recorded, untrusted state, so it keeps the
    # confined profile no matter what the member configured.
    if read_only:
        return "workspace"
    return "off" if policy.filesystem_access == "host" else "workspace"


def _event_kind_for_tool(tool_kind: str) -> AgentEventKind:
    if tool_kind == "execute":
        return AgentEventKind.COMMAND
    if tool_kind in _FILE_CHANGE_KINDS:
        return AgentEventKind.FILE_CHANGE
    return AgentEventKind.TOOL


def _tool_command(update: dict[str, Any]) -> str:
    raw = _dict(update.get("rawInput"))
    command = raw.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return ""


def _turn_usage_events(update: dict[str, Any], session_id: str) -> list[AgentEvent]:
    """Normalize the xAI ``turn_completed`` token counts to the shared keys."""
    raw = _dict(update.get("usage"))
    usage: dict[str, int] = {}
    for source, target in (
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("cachedReadTokens", "cached_input_tokens"),
        ("reasoningTokens", "reasoning_output_tokens"),
        ("totalTokens", "total_tokens"),
    ):
        value = _positive_int(raw.get(source))
        if value is not None:
            usage[target] = value
    if not usage:
        return []
    details: dict[str, Any] = {"stop_reason": update.get("stop_reason")}
    for key in ("costUsdTicks", "modelCalls", "apiDurationMs"):
        if key in raw:
            # Cost and timing are not token counts and must not be summed with
            # usage anywhere downstream.
            details[key] = raw[key]
    return [
        AgentEvent(
            AgentEventKind.USAGE,
            "turn",
            provider_session_id=session_id,
            usage=usage,
            details=details,
        )
    ]


def _retry_state_events(update: dict[str, Any], session_id: str) -> list[AgentEvent]:
    state = _dict(update.get("retryState")) or update
    if not bool(state.get("is_rate_limited")):
        return []
    return [
        AgentEvent(
            AgentEventKind.FAILED,
            "rate_limited",
            message="Grok reported a rate limit while retrying.",
            provider_session_id=session_id,
            details={
                "exhausted": bool(state.get("exhausted")),
                "error_type": state.get("error_type"),
                "max_retries": state.get("max_retries"),
            },
        )
    ]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)
    return ""


def _stop_reason(response: Any) -> str:
    return str(_dict(response).get("stopReason", "") or "")


def _stop_reason_error(stop_reason: str) -> AgentRuntimeError:
    category, message = _STOP_REASONS.get(
        stop_reason,
        (
            AgentRuntimeErrorCategory.PROTOCOL,
            f"Grok returned an unknown stop reason '{stop_reason or 'missing'}'.",
        ),
    )
    return AgentRuntimeError(
        category,
        message,
        details={"stop_reason": stop_reason or "missing"},
        rotate_session=True,
    )


def _agent_error_from_rpc(exc: RpcError) -> AgentRuntimeError:
    error = _dict(exc.error)
    data = _dict(error.get("data"))
    identifiers = {
        str(value).lower()
        for value in (
            error.get("type"),
            data.get("type"),
            data.get("code"),
            data.get("error_type"),
        )
        if value is not None
    }
    details: dict[str, Any] = {
        "provider_code": error.get("code"),
        "provider_type": data.get("type") or error.get("type"),
    }
    for source, target in (
        ("resetAt", "retry_after_at"),
        ("reset_at", "retry_after_at"),
        ("retryAfterSeconds", "retry_after_seconds"),
        ("retry_after_seconds", "retry_after_seconds"),
    ):
        if source in data:
            details[target] = data[source]
    if identifiers & _AUTH_CODES:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            "Grok Build authentication failed.",
            details=details,
        )
    if identifiers & _RATE_LIMIT_CODES:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.RATE_LIMITED,
            "Grok account rate limit is active.",
            details=details,
        )
    if error.get("code") == METHOD_NOT_FOUND:
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
            "The installed Grok Build does not support the required ACP method.",
            details=details,
        )
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.PROTOCOL,
        "Grok Build returned a protocol error.",
        details=details,
        rotate_session=True,
    )


async def _publish(emit: EventSink, event: AgentEvent) -> None:
    result = emit(event)
    if asyncio.iscoroutine(result):
        await result


def _reject_option_id(options: Any) -> str:
    """Return the option id of the agent's own reject option, if it offered one."""
    if not isinstance(options, list):
        return ""
    for wanted in _REJECT_KINDS:
        for option in options:
            candidate = _dict(option)
            if str(candidate.get("kind", "")) == wanted:
                option_id = str(candidate.get("optionId", "") or "")
                if option_id:
                    return option_id
    return ""


def _payload_fields(payload: dict[str, Any]) -> list[str]:
    """Return the payload's top-level field names, never its values."""
    return sorted(str(key)[:_MAX_FIELD_NAME] for key in payload)[:_MAX_FIELDS]


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
