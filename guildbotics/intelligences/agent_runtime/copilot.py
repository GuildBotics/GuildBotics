"""GitHub Copilot CLI Agent Client Protocol (ACP) adapter.

Verified against GitHub Copilot CLI 1.0.77 (``copilot --acp``): ACP protocol
version 1, ``loadSession: true`` with no ``sessionCapabilities.resume``, and a
single ``copilot-login`` authentication method. The model, the reasoning effort
and the approval policy are session configuration options rather than launch
flags, so this adapter re-applies them on every turn and reports back the values
the session actually ended up with.

Only the Copilot-specific half lives here; the protocol itself is in
:mod:`guildbotics.intelligences.agent_runtime.acp`.
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import Any

from guildbotics.intelligences.agent_runtime.acp import AcpAdapterBase, as_dict
from guildbotics.intelligences.agent_runtime.jsonrpc import RpcError
from guildbotics.intelligences.agent_runtime.models import (
    SETTINGS_SCOPE_TURN,
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.agent_runtime.policy import AdapterFilesystemPolicy

#: The only method Copilot advertises. Its ``_meta.terminal-auth`` tells the
#: client to run ``copilot login`` in a terminal, so this adapter can verify a
#: saved login but must never drive the sign-in itself.
_LOGIN_METHOD = "copilot-login"
#: A logged-in install answers ``authenticate`` at once. Anything slower is the
#: interactive terminal login waiting for a user who is not there.
_AUTH_TIMEOUT = 20.0
#: Session configuration options this adapter sets. `model` and
#: `reasoning_effort` are the effort mapping's keys; `allow_all` is the approval
#: policy, which GuildBotics owns rather than exposing as a member setting.
_ALLOW_ALL_OPTION = "allow_all"
_SETTING_KEYS = frozenset({"model", "reasoning_effort"})
#: Copilot spells its weekly quota exhaustion this way; the rest of the shared
#: identifiers are matched by the base adapter.
_RATE_LIMIT_CODES = frozenset({"user_weekly_rate_limited"})
_LOGGER = getLogger(__name__)


class CopilotAcpAdapter(AcpAdapterBase):
    name = "copilot-acp"
    agent_label = "Copilot"
    product_label = "GitHub Copilot CLI"
    # Copilot accepts `session/set_config_option` on any live session, including
    # one that was just reloaded, so changing the model or the effort costs a
    # request rather than a fresh session.
    settings_scope = SETTINGS_SCOPE_TURN
    setting_keys = _SETTING_KEYS
    rate_limit_codes = _RATE_LIMIT_CODES

    def __init__(
        self,
        *,
        executable: str = "copilot",
        timeout: float = 3600.0,
        policy: AdapterFilesystemPolicy | None = None,
    ) -> None:
        super().__init__(executable=executable, timeout=timeout, policy=policy)

    def _launch_argv(self, context: AgentExecutionContext) -> tuple[str, ...]:
        return (
            self._executable,
            "--acp",
            # Headless runs must never let the CLI update itself mid-session.
            "--no-auto-update",
            # A member's session must not be readable or steerable from GitHub
            # web and mobile: it carries workspace contents and takes its
            # instructions from GuildBotics alone.
            "--no-remote-export",
            # Copilot confines file access to the working directory unless this
            # is passed, which is exactly the public policy setting. A read-only
            # turn keeps the confined scope whatever the member configured: it
            # reads untrusted recorded state, and its own reply is an exfil
            # channel that declining writes does not close.
            *(
                ("--allow-all-paths",)
                if _allows_host_paths(self._policy, context)
                else ()
            ),
        )

    def _policy_details(self, context: AgentExecutionContext) -> dict[str, Any]:
        return {
            "filesystem_access": self._policy.filesystem_access,
            "allowed_paths": (
                "host" if _allows_host_paths(self._policy, context) else "workspace"
            ),
            "allow_all": _allow_all(context),
            "read_only": context.read_only,
        }

    async def _authenticate(self, result: dict[str, Any], env: dict[str, str]) -> None:
        methods = [
            str(as_dict(method).get("id", ""))
            for method in result.get("authMethods", [])
            if isinstance(method, dict)
        ]
        if _LOGIN_METHOD not in methods:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "GitHub Copilot CLI has no usable saved login. "
                "Run 'copilot login' as this user.",
                details={"advertised_methods": methods},
            )
        try:
            await asyncio.wait_for(
                # The transport's own deadline would report this as a stalled
                # process; a pending sign-in is an authentication problem.
                self._transport.request(
                    "authenticate", {"methodId": _LOGIN_METHOD}, timeout=None
                ),
                timeout=_AUTH_TIMEOUT,
            )
        except (RpcError, TimeoutError) as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.AUTHENTICATION,
                "GitHub Copilot CLI authentication failed. "
                "Run 'copilot login' as this user.",
                details={"auth_method": _LOGIN_METHOD, "provider_error": str(exc)},
            ) from exc
        # Only the method identifier is recorded; credential values and the
        # contents of the Copilot auth store are never read.
        self._auth_method = _LOGIN_METHOD

    async def _configure_session(
        self, session_id: str, context: AgentExecutionContext, result: dict[str, Any]
    ) -> list[AgentEvent]:
        desired = {
            key: str(value) for key, value in self.applied_settings(context).items()
        }
        desired[_ALLOW_ALL_OPTION] = _allow_all(context)
        current = _config_values(result)
        for option_id, value in desired.items():
            if current.get(option_id) == value:
                continue
            try:
                updated = _config_values(
                    await self._transport.request(
                        "session/set_config_option",
                        {
                            "sessionId": session_id,
                            "configId": option_id,
                            "value": value,
                        },
                    )
                )
            except RpcError as exc:
                raise self._agent_error_from_rpc(exc) from exc
            # Copilot answers an unknown option id with an empty result instead
            # of an error, so only an option list it returns proves anything.
            if updated:
                current = updated
        rejected = sorted(
            option_id
            for option_id, value in desired.items()
            if current.get(option_id) != value
        )
        if _ALLOW_ALL_OPTION in rejected and context.read_only:
            # Every other setting only degrades the turn, but this one is what
            # holds a read-only turn back. A session that kept `on` from an
            # earlier turn would act without asking, so an unconfirmed `off` is
            # a refusal to run rather than a warning.
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Copilot did not confirm the read-only approval policy.",
                details={
                    "requested": desired[_ALLOW_ALL_OPTION],
                    "effective": current.get(_ALLOW_ALL_OPTION, ""),
                },
                rotate_session=True,
            )
        if rejected:
            _LOGGER.warning(
                "Copilot did not apply the requested session settings: %s",
                ", ".join(rejected),
            )
        return [
            AgentEvent(
                AgentEventKind.PROCESS,
                "settings",
                provider_session_id=session_id,
                details={
                    # The effective values Copilot reports back, never a guess
                    # from what was requested.
                    "model": current.get("model", ""),
                    "reasoning_effort": current.get("reasoning_effort", ""),
                    _ALLOW_ALL_OPTION: current.get(_ALLOW_ALL_OPTION, ""),
                    "requested": desired,
                    "rejected": rejected,
                },
            )
        ]


def _allows_host_paths(
    policy: AdapterFilesystemPolicy, context: AgentExecutionContext
) -> bool:
    """Whether this turn may reach files outside the working directory.

    A read-only turn only inspects recorded, untrusted state, so it keeps the
    confined scope no matter what the member configured. Declining its writes is
    not enough on its own: reads inside the allowed paths never ask, and the
    turn's own reply would carry whatever it read back out.
    """
    return policy.filesystem_access == "host" and not context.read_only


def _allow_all(context: AgentExecutionContext) -> str:
    """Whether Copilot may run tools without asking, for this turn.

    A read-only turn only inspects recorded, untrusted state, so Copilot has to
    ask before every write, shell command and URL fetch -- and the base adapter
    declines every one of them. Reads inside the allowed paths never ask, so the
    turn can still do its job.
    """
    return "off" if context.read_only else "on"


def _config_values(result: Any) -> dict[str, str]:
    """The current value of every config option in a session response."""
    options = as_dict(result).get("configOptions")
    if not isinstance(options, list):
        return {}
    return {
        str(as_dict(option)["id"]): str(as_dict(option).get("currentValue", "") or "")
        for option in options
        if as_dict(option).get("id")
    }
