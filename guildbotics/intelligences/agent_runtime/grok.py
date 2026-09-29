"""Grok Build Agent Client Protocol (ACP) adapter.

Verified against Grok Build 1.0.34 (``grok agent stdio``): ACP protocol
version 1, ``loadSession: true``, ``sessionCapabilities.resume: {}``, and the
authentication method an external auth provider command adds. The adapter gates on
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
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    context_compaction_event,
)

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
        "_x.ai/queue/changed",
        "_x.ai/session/prompt_complete",
        "_x.ai/sessions/changed",
        "_x.ai/settings/update",
    }
)
#: The ACP session options this adapter can set on Grok Build.
_EFFORT_SETTING_KEYS = frozenset({"model", "reasoning_effort"})


class GrokAcpAdapter(AcpAdapterBase):
    name = "grok-acp"
    agent_label = "Grok"
    product_label = "Grok Build"
    tool_name = "grok"
    # The process starts for each turn. Session options must be confirmed after
    # session/new or session/resume: launch flags alone can be overwritten.
    setting_keys = _EFFORT_SETTING_KEYS
    extension_notifications = _EXTENSION_NOTIFICATIONS
    known_extension_noise = _KNOWN_EXTENSION_NOISE

    def __init__(
        self,
        *,
        executable: str = "grok",
        timeout: float = 3600.0,
    ) -> None:
        super().__init__(executable=executable, timeout=timeout)
        self._turn_model = ""
        self._confirmed_model = ""
        self._turn_effort = ""

    def _launch_argv(self, context: AgentExecutionContext) -> tuple[str, ...]:
        return (
            self._executable,
            # A headless turn must not update the CLI while it runs.
            "--no-auto-update",
            "--sandbox",
            _SANDBOX_PROFILE,
            "agent",
            # The root parser accepts this flag too, but only the agent parser
            # passes it through to the ACP session.
            "--always-approve",
            "stdio",
        )

    async def _configure_session(
        self,
        session_id: str,
        context: AgentExecutionContext,
        result: dict[str, Any],
    ) -> list[AgentEvent]:
        desired = {
            key: str(value) for key, value in self.applied_settings(context).items()
        }
        try:
            current = await self._set_config_options(session_id, desired, result)
        except RpcError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.CONFIGURATION,
                "Grok did not apply the requested session settings.",
                details={"provider_error": str(exc)},
            ) from exc
        for option_id, value in desired.items():
            if current.get(option_id) != value:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.CONFIGURATION,
                    f"Grok did not confirm {option_id} for the session.",
                )
        self._confirmed_model = current.get("model", "")
        self._turn_effort = current.get("reasoning_effort", "")
        return []

    def _policy_details(self, context: AgentExecutionContext) -> dict[str, Any]:
        return {"sandbox": _SANDBOX_PROFILE}

    async def _prepare_turn(self, context: AgentExecutionContext) -> None:
        await super()._prepare_turn(context)
        self._turn_model = ""
        self._confirmed_model = ""
        self._turn_effort = ""

    def _effective_settings(self, context: AgentExecutionContext) -> tuple[str, str]:
        # The turn's model update wins over the option confirmed after resume.
        # Initialize's modelState can differ from both. Reasoning effort comes
        # from the confirmed session option.
        return self._turn_model or self._confirmed_model, self._turn_effort

    def _decode(
        self, method: str, params: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        if method == "_x.ai/models/update":
            model = params.get("currentModelId")
            if isinstance(model, str) and model:
                self._turn_model = model
            return []
        return super()._decode(method, params, session_id)

    def _agent_version_of(self, result: dict[str, Any]) -> str:
        # Grok Build reports its version privately rather than in `agentInfo`.
        return str(as_dict(result.get("_meta")).get("agentVersion", ""))

    async def _authenticate(self, result: dict[str, Any]) -> None:
        # A turn's login is lent to it: Grok Build takes the stand-in from the
        # external auth provider command the environment names, and offers
        # that as a method of its own. Nothing else is usable in a turn -- an
        # interactive sign-in has no one to answer it, and an API key would
        # have to reach the environment, which it never does.
        methods = [as_dict(method) for method in result.get("authMethods", [])]
        chosen = next(
            (
                str(method.get("id", ""))
                for method in methods
                if as_dict(method.get("_meta")).get("external_provider") is True
            ),
            "",
        )
        if not chosen:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "Grok Build did not offer the login GuildBotics lends a turn.",
                details={"advertised_methods": [str(m.get("id", "")) for m in methods]},
            )
        try:
            await self._transport.request("authenticate", {"methodId": chosen})
        except RpcError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "Grok Build authentication failed.",
                details={"auth_method": chosen, "provider_error": str(exc)},
            ) from exc
        # Only the method identifier is recorded.
        self._auth_method = chosen

    def _decode_extension(
        self, update: dict[str, Any], session_id: str
    ) -> list[AgentEvent]:
        kind = str(update.get("sessionUpdate", "") or "")
        if kind in _COMPACTION_UPDATES:
            return [context_compaction_event(session_id, {"detected_by": kind})]
        if kind == "retry_state":
            return _retry_state_events(update, session_id)
        if kind == "turn_completed":
            # Grok Build 0.2.114 never emits the standard ACP usage_update; the
            # only token counts it reports arrive here.
            return _turn_usage_events(update, session_id)
        return super()._decode_extension(update, session_id)


#: Grok's own sandbox is off inside the agent environment: its Linux
#: profiles need Landlock, which the environment's kernel does not have, and
#: Grok refuses to start rather than run a profile it cannot enforce. The
#: environment is the boundary; the inner sandbox was never counted as one.
_SANDBOX_PROFILE = "off"


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
