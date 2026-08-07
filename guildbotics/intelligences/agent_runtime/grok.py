"""Grok Build Agent Client Protocol (ACP) adapter.

Verified against Grok Build 0.2.118 (``grok agent stdio``): ACP protocol
version 1, ``loadSession: true`` with no ``sessionCapabilities.resume``, and
the ``cached_token`` / ``grok.com`` authentication methods. The adapter gates on
those advertised capabilities rather than on the version string, so a newer
Grok Build that still speaks ACP v1 keeps working.

Only the xAI-specific half lives here; the protocol itself is in
:mod:`guildbotics.intelligences.agent_runtime.acp`.
"""

from __future__ import annotations

from typing import Any

from guildbotics.intelligences.agent_runtime.acp import (
    AcpAdapterBase,
    as_dict,
    non_negative_int,
)
from guildbotics.intelligences.agent_runtime.jsonrpc import RpcError
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
)
from guildbotics.intelligences.agent_runtime.policy import AdapterFilesystemPolicy

_CACHED_TOKEN_METHOD = "cached_token"
_API_KEY_METHOD = "xai.api_key"
#: Interactive sign-in must never be started from a headless run.
_INTERACTIVE_AUTH_METHODS = frozenset({"grok.com"})

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
#: The launch options this adapter can set on `grok agent stdio`.
_EFFORT_SETTING_KEYS = frozenset({"model", "reasoning_effort"})
_MEMBER_TOOL_INSTRUCTION = """<guildbotics_member_transport>
A trusted MCP tool named `guildbotics_member` is available. Use it for every
command documented as `guildbotics member ...`; never run those commands in the
terminal. Pass the exact CLI tokens after `member` as `arguments`, without shell
quoting, pass `--content-stdin` data through the tool's `stdin` field, and set
`turn_grant` to `{turn_grant}`. This grant is valid only for this turn.
</guildbotics_member_transport>"""


class GrokAcpAdapter(AcpAdapterBase):
    name = "grok-acp"
    agent_label = "Grok"
    product_label = "Grok Build"
    # `grok agent stdio` takes the model and reasoning effort as launch options,
    # so they are fixed for the life of the process: changing them needs a fresh
    # session rather than a mid-conversation adjustment.
    settings_scope = SETTINGS_SCOPE_SESSION
    setting_keys = _EFFORT_SETTING_KEYS
    extension_notifications = _EXTENSION_NOTIFICATIONS
    known_extension_noise = _KNOWN_EXTENSION_NOISE

    def __init__(
        self,
        *,
        executable: str = "grok",
        timeout: float = 3600.0,
        policy: AdapterFilesystemPolicy | None = None,
    ) -> None:
        super().__init__(executable=executable, timeout=timeout, policy=policy)
        self._member_broker = MemberCapabilityBroker()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self._member_broker.close()

    async def _prepare_turn(self, context: AgentExecutionContext) -> None:
        mcp = as_dict(self._capabilities.get("mcpCapabilities"))
        if not mcp.get("http"):
            await self.close()
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
                "The installed Grok Build does not support HTTP MCP servers.",
                details={"agent_version": self._agent_version},
            )
        try:
            await self._member_broker.activate(context)
        except MemberCapabilityBrokerError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Could not start the trusted member capability broker.",
            ) from exc

    async def _finish_turn(self, context: AgentExecutionContext) -> None:
        await self._member_broker.deactivate(context)

    def _agent_member_environment(
        self, context: AgentExecutionContext
    ) -> dict[str, str]:
        # The Grok process receives neither the execution lease nor the
        # delegation grant. Only the authenticated broker holds them.
        return {}

    def _mcp_servers(self, context: AgentExecutionContext) -> list[dict[str, Any]]:
        return [self._member_broker.mcp_server]

    def _turn_prompt(self, prompt: str, context: AgentExecutionContext) -> str:
        instruction = _MEMBER_TOOL_INSTRUCTION.format(
            turn_grant=self._member_broker.turn_grant
        )
        return f"{instruction}\n\n{prompt}"

    def _launch_argv(self, context: AgentExecutionContext) -> tuple[str, ...]:
        return _launch_argv(
            self._executable,
            self._policy,
            context.read_only,
            self.applied_settings(context),
        )

    def _policy_details(self, context: AgentExecutionContext) -> dict[str, Any]:
        return {
            "filesystem_access": self._policy.filesystem_access,
            "sandbox": _sandbox_profile(self._policy, context.read_only),
        }

    def _effective_settings(self, context: AgentExecutionContext) -> tuple[str, str]:
        # Grok Build names the model the process is fixed to in its
        # `initialize` response (`_meta.modelState.currentModelId`, observed on
        # 0.2.114), which also covers a launch that imposed none and ran on the
        # account default. The reasoning effort is reported nowhere, so only a
        # launch option that imposed it is known.
        applied = self.applied_settings(context)
        reported = str(
            as_dict(
                as_dict(self._initialize_result.get("_meta")).get("modelState")
            ).get("currentModelId", "")
            or ""
        )
        return (
            reported or str(applied.get("model", "") or ""),
            str(applied.get("reasoning_effort", "") or ""),
        )

    def _agent_version_of(self, result: dict[str, Any]) -> str:
        # Grok Build reports its version privately rather than in `agentInfo`.
        return str(as_dict(result.get("_meta")).get("agentVersion", ""))

    async def _authenticate(self, result: dict[str, Any], env: dict[str, str]) -> None:
        methods = [
            str(as_dict(method).get("id", ""))
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
        return super()._decode_extension(update, session_id)


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
        "agent",
        # These are `grok agent` options. The root parser accepts the same
        # spellings, but does not propagate always-approve to ACP sessions.
        "--always-approve",
        *options,
        "stdio",
    )


def _sandbox_profile(policy: AdapterFilesystemPolicy, read_only: bool = False) -> str:
    # A read-only turn only inspects recorded, untrusted state, so it keeps the
    # confined profile no matter what the member configured.
    if read_only:
        return "workspace"
    return "off" if policy.filesystem_access == "host" else "workspace"


def _turn_usage_events(update: dict[str, Any], session_id: str) -> list[AgentEvent]:
    """Normalize the xAI ``turn_completed`` token counts to the shared keys."""
    raw = as_dict(update.get("usage"))
    usage: dict[str, int] = {}
    for source, target in (
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("cachedReadTokens", "cached_input_tokens"),
        ("reasoningTokens", "reasoning_output_tokens"),
        ("totalTokens", "total_tokens"),
    ):
        value = non_negative_int(raw.get(source))
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
    state = as_dict(update.get("retryState")) or update
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
