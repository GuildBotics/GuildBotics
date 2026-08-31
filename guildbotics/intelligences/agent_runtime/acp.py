"""Agent Client Protocol (ACP) v1 client shared by the native ACP adapters.

Everything ACP itself defines lives here: the ``initialize`` handshake, session
creation and exact reload, the ``session/prompt`` turn loop, the standard
``session/update`` decoding, the rejection of permission requests, and the
mapping from JSON-RPC errors to :class:`AgentRuntimeError`. A provider subclass
supplies only what the protocol leaves open -- how its CLI is launched and
authenticated, how a session is configured, and which private notification
channels it adds on top of the standard ones.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from logging import getLogger
from typing import Any

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    create_agent_subprocess,
    isolated_agent_environment,
    remove_isolated_config,
    terminate_process_tree,
)
from guildbotics.intelligences.agent_runtime.jsonrpc import (
    FATAL_NOTIFICATION,
    METHOD_NOT_FOUND,
    LineJsonRpcTransport,
    RpcError,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
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

ACP_PROTOCOL_VERSION = 1
#: Version of the GuildBotics client contract, matching the Codex adapter's.
CLIENT_VERSION = "1"

_APPROVAL_NOTIFICATION = "guildbotics/approval"
#: Standard ACP updates GuildBotics deliberately ignores; they describe the
#: peer's own UI affordances rather than the turn.
_IGNORED_UPDATES = frozenset(
    {
        "available_commands_update",
        "current_mode_update",
        "config_option_update",
    }
)
#: Standard ACP updates that carry no information of their own for a turn: the
#: prompt is already known and replayed history is absorbed by count.
_SILENT_UPDATES = frozenset({"user_message_chunk", "session_info_update"})
_STOP_REASONS = {
    "cancelled": (AgentRuntimeErrorCategory.CANCELLED, "{agent} cancelled the turn."),
    "max_tokens": (
        AgentRuntimeErrorCategory.PROCESS,
        "{agent} stopped the turn at the token limit.",
    ),
    "max_turn_requests": (
        AgentRuntimeErrorCategory.PROCESS,
        "{agent} stopped the turn at the request limit.",
    ),
    "refusal": (AgentRuntimeErrorCategory.PROCESS, "{agent} refused the turn."),
}
#: Error identifiers every provider spells the same way. A subclass adds its
#: own through ``rate_limit_codes`` / ``auth_codes``.
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
#: Tool kinds that actually change the workspace. ACP `locations` reports every
#: file a tool touched, including reads, so it must not drive this.
_FILE_CHANGE_KINDS = frozenset({"edit", "delete", "move"})
#: Preferred first: declining once must not memorize the decision.
_REJECT_KINDS = ("reject_once", "reject_always")
_LOGGER = getLogger(__name__)
_MAX_FIELDS = 20
_MAX_FIELD_NAME = 64


class AcpAdapterBase:
    """The ACP v1 half of a native adapter, minus everything provider-specific.

    Subclasses set the class attributes below and implement the hooks at the
    end of the class. The public behaviour -- event stream, error categories,
    usage keys -- is fixed here so every ACP provider reports the same shapes.
    """

    #: Adapter identifier recorded on conversations, e.g. ``"grok-acp"``.
    name = ""
    #: The agent as it acts within a turn ("Grok", "Copilot").
    agent_label = ""
    #: The installed CLI product ("Grok Build", "GitHub Copilot CLI").
    product_label = ""
    settings_scope = SETTINGS_SCOPE_SESSION
    #: The provider settings keys this adapter can really impose.
    setting_keys: frozenset[str] = frozenset()
    #: Private notification channels this provider wraps session updates in.
    extension_notifications: frozenset[str] = frozenset()
    #: Private channels known to carry only peer UI state. They are counted so
    #: a change stays visible, but never sampled.
    known_extension_noise: frozenset[str] = frozenset()
    #: Provider error identifiers, added to the shared sets above.
    rate_limit_codes: frozenset[str] = frozenset()
    auth_codes: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        executable: str,
        timeout: float = 3600.0,
        policy: AdapterFilesystemPolicy | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._transport = LineJsonRpcTransport(
            label=f"{self.product_label} ACP",
            include_version=True,
            request_timeout=min(timeout, 30.0),
            on_reverse_request=self._handle_agent_request,
        )
        self._policy = policy or AdapterFilesystemPolicy()
        self._gh_config_dir = ""
        self._capabilities: dict[str, Any] = {}
        self._agent_version = ""
        #: The running process's full `initialize` response. Some providers
        #: report state here that arrives nowhere else, such as the model the
        #: process is currently fixed to.
        self._initialize_result: dict[str, Any] = {}
        self._auth_method = ""
        self._active_session_id = ""
        #: The command the running process was started with. A turn whose launch
        #: command differs cannot reuse it: the boundary is fixed at startup.
        self._launched_argv: tuple[str, ...] = ()
        #: Sessions the running process already holds open. Reloading one of
        #: them is at best a wasted replay and at worst an error, so the load
        #: only happens when this process does not have the session yet.
        self._open_sessions: set[str] = set()
        self._unhandled: dict[str, dict[str, Any]] = {}
        self._tool_kinds: dict[str, str] = {}
        self._context_used = 0
        self._context_size = 0
        #: The structured rate-limit notice the current turn received, if any.
        #: Providers may end a rate-limited turn with a bare protocol error, so
        #: the notice is what classifies that terminal failure.
        self._turn_rate_limit: dict[str, Any] = {}
        self._member_broker = MemberCapabilityBroker()

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        """The settings this adapter can really impose, normalized.

        Silent by design: it also backs the session fingerprint, which is
        computed outside the run path and must not emit a second round of
        warnings.
        """
        return {
            key: value
            for key, value in context.provider_options.items()
            if key in self.setting_keys and str(value or "")
        }

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        try:
            try:
                await self._member_broker.activate(context)
            except MemberCapabilityBrokerError as exc:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROCESS,
                    "Could not start the trusted member capability broker.",
                ) from exc
            await self._ensure_started(context, emit)
            try:
                await self._prepare_turn(context)
                return await self._run_active_turn(prompt, context, conversation, emit)
            finally:
                await self._finish_turn(context)
        finally:
            await self._member_broker.deactivate(context)

    async def _run_active_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        self._warn_unusable_settings(context)
        self._unhandled = {}
        self._tool_kinds = {}
        self._context_used = 0
        self._context_size = 0
        self._turn_rate_limit = {}
        await _publish(
            emit,
            AgentEvent(
                AgentEventKind.APPROVAL,
                "policy",
                approval="never",
                details=self._policy_details(context),
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
                    "prompt": [
                        {"type": "text", "text": self._turn_prompt(prompt, context)}
                    ],
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
            raise self._agent_error_from_rpc(exc) from exc
        except TimeoutError as exc:
            await self.interrupt()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"{self.agent_label} turn timed out.",
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
            raise self._stop_reason_error(stop_reason)
        if not output:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                f"{self.agent_label} completed the turn without a terminal response.",
                rotate_session=True,
            )
        if self._context_size > 0:
            usage["context_used_tokens"] = self._context_used
            usage["context_size_tokens"] = self._context_size
        effective_model, effective_effort = self._effective_settings(context)
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
            model=effective_model,
            effort=effective_effort,
        )

    async def interrupt(self) -> None:
        await self._member_broker.deactivate()
        if self._active_session_id:
            with suppress(asyncio.CancelledError, Exception):
                await self._transport.notify(
                    "session/cancel", {"sessionId": self._active_session_id}
                )
        process = self._transport.process
        if process is not None and process.returncode is None:
            await terminate_process_tree(process)

    async def close(self) -> None:
        try:
            await self._close_provider()
        finally:
            await self._member_broker.close()

    async def _close_provider(self) -> None:
        """Stop only the ACP provider while preserving the active broker."""
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
        argv = self._launch_argv(context)
        # The launch command carries the boundary the provider can only be given
        # at startup -- the sandbox profile, the allowed paths, the model a
        # session-scoped provider is fixed to. A running process was given the
        # previous turn's, so reusing it for a turn that asks for a narrower one
        # would enforce the wider boundary while reporting the narrower.
        if self._transport.running and argv == self._launched_argv:
            return
        if self._transport.process is not None:
            await self._close_provider()
        cwd = context.cwd
        env, self._gh_config_dir = isolated_agent_environment()
        try:
            process = await create_agent_subprocess(
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
                f"Could not start {self.product_label}: {exc}",
            ) from exc
        self._transport.start(process)
        self._launched_argv = argv
        # A fresh process holds nothing, whatever the previous one held. A
        # session the conversation still names is reloaded on the way in.
        self._open_sessions = set()
        try:
            await self._initialize()
        except AgentRuntimeError:
            await self.close()
            raise
        except RpcError as exc:
            await self.close()
            raise self._agent_error_from_rpc(exc) from exc
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

    async def _initialize(self) -> None:
        result = as_dict(
            await self._transport.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    # Only capabilities GuildBotics actually implements are
                    # declared; an undeclared capability would let the agent
                    # call back into a client service that does not exist.
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
                f"The installed {self.product_label} does not speak "
                "Agent Client Protocol v1.",
                details={"protocol_version": result.get("protocolVersion")},
            )
        self._initialize_result = result
        self._capabilities = as_dict(result.get("agentCapabilities"))
        self._agent_version = self._agent_version_of(result)
        if not self._capabilities.get("loadSession") and not self._supports_resume:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                f"The installed {self.product_label} cannot resume an exact session.",
                details={"agent_version": self._agent_version},
            )
        await self._authenticate(result)

    @property
    def _supports_resume(self) -> bool:
        # An advertised-but-disabled capability is a refusal, not an offer:
        # calling session/resume on it would only earn a protocol error.
        return bool(
            as_dict(self._capabilities.get("sessionCapabilities")).get("resume")
        )

    async def _resolve_session(
        self,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> str:
        if not conversation.provider_session_id:
            try:
                result = as_dict(
                    await self._transport.request(
                        "session/new",
                        {
                            "cwd": str(context.cwd),
                            "mcpServers": self._mcp_servers(context),
                        },
                    )
                )
            except RpcError as exc:
                raise self._agent_error_from_rpc(exc) from exc
            session_id = str(result.get("sessionId", "") or "")
            if not session_id:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROTOCOL,
                    f"{self.agent_label} returned no session id.",
                    rotate_session=True,
                )
            self._open_sessions.add(session_id)
            await self._publish_session_settings(session_id, context, result, emit)
            return session_id
        session_id = conversation.provider_session_id
        if session_id in self._open_sessions:
            # The conversation never left this process, so there is no history
            # to rehydrate and nothing to reload -- Copilot even refuses the
            # second load with "already loaded". The turn's settings are still
            # re-applied, from no known current state.
            await self._publish_session_settings(session_id, context, {}, emit)
            return session_id
        method = "session/resume" if self._supports_resume else "session/load"
        try:
            result = as_dict(
                await self._transport.request(
                    method,
                    {
                        "sessionId": session_id,
                        "cwd": str(context.cwd),
                        "mcpServers": self._mcp_servers(context),
                    },
                )
            )
        except RpcError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.SESSION_UNAVAILABLE,
                f"The exact {self.agent_label} session could not be resumed.",
                details={"provider_error": str(exc), "session_method": method},
                rotate_session=True,
            ) from exc
        self._open_sessions.add(session_id)
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
        await self._publish_session_settings(session_id, context, result, emit)
        return session_id

    async def _publish_session_settings(
        self,
        session_id: str,
        context: AgentExecutionContext,
        result: dict[str, Any],
        emit: EventSink,
    ) -> None:
        for event in await self._configure_session(session_id, context, result):
            await _publish(emit, event)

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
            params = as_dict(message.get("params"))
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
                if event.kind is AgentEventKind.FAILED and event.name == "rate_limited":
                    self._turn_rate_limit = dict(event.details)

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
                                f"{self.product_label} stopped unexpectedly.",
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
            return self._decode_update(as_dict(params.get("update")), session_id)
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
        if method in self.extension_notifications:
            return self._decode_extension(as_dict(params.get("update")), session_id)
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
        if kind in _SILENT_UPDATES:
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
            str(as_dict(location).get("path", ""))
            for location in update.get("locations", [])
            if isinstance(location, dict) and as_dict(location).get("path")
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
        used = non_negative_int(update.get("used"))
        size = non_negative_int(update.get("size"))
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
            # This works even if the agent renames or drops its own notification.
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
        session_updates = self.extension_notifications | {"session/update"}
        replayed = 0
        for message in self._transport.drain_notifications():
            method = str(message.get("method", ""))
            if method not in session_updates:
                if method.startswith("_"):
                    self._note_unhandled(method, as_dict(message.get("params")))
                continue
            # Replayed session updates are history on both the standard and the
            # extension channels. They are counted, never decoded: a previous
            # turn's usage must not be reported as this turn's.
            replayed += 1
            update = as_dict(as_dict(message.get("params")).get("update"))
            if str(update.get("sessionUpdate", "")) != "usage_update":
                continue
            used = non_negative_int(update.get("used"))
            size = non_negative_int(update.get("size"))
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
        if key not in self.known_extension_noise and "fields" not in entry:
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
            # The provider's own boundary plus the startup approval policy is
            # the contract; an unexpected request means the agent wants
            # something outside it. The option id is chosen by the agent per
            # request, so it has to be read back out of the request rather than
            # assumed from the option kind.
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
                        "toolCall": as_dict(params.get("toolCall")).get("title", ""),
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

    def _warn_unusable_settings(self, context: AgentExecutionContext) -> None:
        """Report requested settings this adapter cannot act on."""
        unknown = sorted(set(context.provider_options) - self.setting_keys)
        if unknown:
            _LOGGER.warning(
                "Ignoring unsupported %s effort settings: %s",
                self.agent_label,
                ", ".join(unknown),
            )

    def _stop_reason_error(self, stop_reason: str) -> AgentRuntimeError:
        category, template = _STOP_REASONS.get(
            stop_reason, (AgentRuntimeErrorCategory.PROTOCOL, "")
        )
        return AgentRuntimeError(
            category,
            template.format(agent=self.agent_label)
            if template
            else f"{self.agent_label} returned an unknown stop reason "
            f"'{stop_reason or 'missing'}'.",
            details={"stop_reason": stop_reason or "missing"},
            rotate_session=True,
        )

    def _agent_error_from_rpc(self, exc: RpcError) -> AgentRuntimeError:
        error = as_dict(exc.error)
        data = as_dict(error.get("data"))
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
        if identifiers & (_AUTH_CODES | self.auth_codes):
            return AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                f"{self.product_label} authentication failed.",
                details=details,
            )
        if identifiers & (_RATE_LIMIT_CODES | self.rate_limit_codes):
            return AgentRuntimeError(
                AgentRuntimeErrorCategory.RATE_LIMITED,
                f"{self.agent_label} account rate limit is active.",
                details=details,
            )
        if error.get("code") == METHOD_NOT_FOUND:
            return AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                f"The installed {self.product_label} does not support the "
                "required ACP method.",
                details=details,
            )
        if self._turn_rate_limit:
            # The provider ended the turn with a bare protocol error after
            # sending a structured rate-limit notice; the notice is the real
            # classification, and it is what routes the workflow into its
            # rate-limit deferral instead of blind retries.
            return AgentRuntimeError(
                AgentRuntimeErrorCategory.RATE_LIMITED,
                f"{self.agent_label} account rate limit is active.",
                details={**self._turn_rate_limit, **details},
            )
        return AgentRuntimeError(
            AgentRuntimeErrorCategory.PROTOCOL,
            f"{self.product_label} returned a protocol error.",
            details=details,
            rotate_session=True,
        )

    # --- provider hooks ------------------------------------------------------

    async def _prepare_turn(self, context: AgentExecutionContext) -> None:
        """Start provider-specific services needed while this turn is active."""
        mcp = as_dict(self._capabilities.get("mcpCapabilities"))
        if not mcp.get("http"):
            await self.close()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                f"The installed {self.product_label} does not support HTTP MCP servers.",
                details={"agent_version": self._agent_version},
            )

    async def _finish_turn(self, context: AgentExecutionContext) -> None:
        """Revoke provider-specific services after this turn finishes."""

    def _mcp_servers(self, context: AgentExecutionContext) -> list[dict[str, Any]]:
        """ACP MCP descriptors attached to a new or reloaded session."""
        return [self._member_broker.mcp_server]

    def _turn_prompt(self, prompt: str, context: AgentExecutionContext) -> str:
        """Add provider-specific execution instructions to the turn prompt."""
        return self._member_broker.prompt(prompt)

    def _launch_argv(self, context: AgentExecutionContext) -> tuple[str, ...]:
        """The command line that starts this provider's ACP server."""
        raise NotImplementedError

    def _policy_details(self, context: AgentExecutionContext) -> dict[str, Any]:
        """The boundary this turn runs under, as reported on the policy event."""
        raise NotImplementedError

    async def _authenticate(self, result: dict[str, Any]) -> None:
        """Establish that the installed CLI has a usable non-interactive login."""
        raise NotImplementedError

    async def _configure_session(
        self, session_id: str, context: AgentExecutionContext, result: dict[str, Any]
    ) -> list[AgentEvent]:
        """Apply this turn's settings to a freshly created or reloaded session.

        ``result`` is the ``session/new`` or ``session/load`` response, which is
        where ACP reports the session's current model and configuration. The
        events returned describe what the session ended up running with.
        """
        return []

    def _effective_settings(self, context: AgentExecutionContext) -> tuple[str, str]:
        """The model and effort the finished turn really ran with.

        ACP itself defines no place for them: one provider reports the values
        back from its session configuration, another is fixed to what its launch
        command imposed. A provider that can establish neither reports nothing,
        because an invented effective value is worse than an absent one.
        """
        return "", ""

    def _decode_extension(
        self, update: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        """Normalize a session update that arrived on a private channel."""
        self._note_unhandled(
            f"extension:{update.get('sessionUpdate', '')}",
            update,
        )
        return []

    def _agent_version_of(self, result: dict[str, Any]) -> str:
        """The installed agent's version as reported by ``initialize``."""
        return str(as_dict(result.get("agentInfo")).get("version", ""))


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


def _event_kind_for_tool(tool_kind: str) -> AgentEventKind:
    if tool_kind == "execute":
        return AgentEventKind.COMMAND
    if tool_kind in _FILE_CHANGE_KINDS:
        return AgentEventKind.FILE_CHANGE
    return AgentEventKind.TOOL


def _tool_command(update: dict[str, Any]) -> str:
    raw = as_dict(update.get("rawInput"))
    command = raw.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return ""


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
    return str(as_dict(response).get("stopReason", "") or "")


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
            candidate = as_dict(option)
            if str(candidate.get("kind", "")) == wanted:
                option_id = str(candidate.get("optionId", "") or "")
                if option_id:
                    return option_id
    return ""


def _payload_fields(payload: dict[str, Any]) -> list[str]:
    """Return the payload's top-level field names, never its values."""
    return sorted(str(key)[:_MAX_FIELD_NAME] for key in payload)[:_MAX_FIELDS]


def non_negative_int(value: Any) -> int | None:
    """The value as a token or size count, or ``None`` when it is neither."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def as_dict(value: Any) -> dict[str, Any]:
    """The value as a mapping, or an empty one -- protocol payloads are untyped."""
    return value if isinstance(value, dict) else {}
