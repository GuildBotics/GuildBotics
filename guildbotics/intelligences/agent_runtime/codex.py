"""Codex App Server JSONL adapter."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from logging import getLogger
from pathlib import Path
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
    MEMBER_BROKER_TOKEN_ENV,
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
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
from guildbotics.intelligences.agent_runtime.usage import parse_codex_rate_limits
from guildbotics.intelligences.cli_agents import (
    resolve_cli_agent_path,
    unsupported_network_reason,
)
from guildbotics.intelligences.sandbox import (
    SandboxContract,
    executable_read_roots,
    redact_path,
)

_MODERN_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)
_LEGACY_APPROVAL_METHODS = frozenset({"execCommandApproval", "applyPatchApproval"})
_UNSUPPORTED_APPROVAL_METHODS = frozenset({"item/permissions/requestApproval"})
_APPROVAL_POLICY = "never"
#: The permission profile GuildBotics defines for every thread it starts. It is
#: passed as configuration overrides at launch, so no user `config.toml`
#: profile is read or written.
_PERMISSION_PROFILE = "guildbotics"
#: Where Codex looks for skills, relative to the home directory (`.agents`)
#: and to its config folder (`skills`, `_skills`). Codex injects only the
#: skill index into the prompt; the agent reads a skill's body itself, from
#: inside the sandbox, so these must be readable or every skill outside the
#: working directory is listed but unusable.
_HOME_SKILL_ROOTS = (".agents/skills",)
_CONFIG_SKILL_ROOTS = ("skills", "_skills")
#: The only effort-mapping keys ``turn/start`` accepts. Anything else is a
#: configuration mistake and is reported rather than silently dropped.
_TURN_SETTING_KEYS = frozenset({"model", "effort"})
_LOGGER = getLogger(__name__)


def _supported_efforts(entry: dict[str, Any]) -> set[str]:
    """The reasoning efforts a model entry advertises.

    Each element is an object describing one level
    (``{"reasoningEffort": "low", "description": ...}``), not a bare string.
    """
    raw = entry.get("supportedReasoningEfforts")
    if not isinstance(raw, list):
        return set()
    return {
        effort
        for item in raw
        if isinstance(item, dict) and (effort := str(item.get("reasoningEffort") or ""))
    }


def _default_entry(catalog: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The model a turn runs on when it names none of its own."""
    for entry in catalog.values():
        if entry.get("isDefault"):
            return entry
    return None


class CodexAppServerAdapter:
    name = "codex-app-server"
    # ``turn/start`` accepts model and effort on every turn, so a change never
    # requires a fresh thread.
    settings_scope = SETTINGS_SCOPE_TURN

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        """The turn/start fields this adapter recognizes.

        Not used for rotation (this adapter is turn-scoped, so a change costs
        nothing), but it keeps the contract uniform across adapters.
        """
        return {
            key: value
            for key, value in context.provider_options.items()
            if key in _TURN_SETTING_KEYS
        }

    def __init__(
        self,
        *,
        executable: str = "codex",
        timeout: float = 3600.0,
    ) -> None:
        self._executable = executable
        self._model_catalog: dict[str, dict[str, Any]] = {}
        self._timeout = timeout
        self._transport = LineJsonRpcTransport(
            label="Codex App Server",
            request_timeout=min(timeout, 30.0),
            on_reverse_request=self._handle_server_request,
        )
        self._gh_config_dir = ""
        self._active_thread_id = ""
        self._active_turn_id = ""
        self._sandbox_overrides: dict[str, Any] = {}
        self._member_broker = MemberCapabilityBroker()

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        try:
            await self._member_broker.activate(context)
        except MemberCapabilityBrokerError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Could not start the trusted member capability broker.",
            ) from exc
        try:
            return await self._run_active_turn(prompt, context, conversation, emit)
        finally:
            await self._member_broker.deactivate(context)

    async def _run_active_turn(
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
            details={
                "requested_policy": context.sandbox.requested_policy(
                    context.cwd, workspace_root=context.workspace_data_root
                ),
                "adapter_settings": _redacted_overrides(
                    self._sandbox_overrides, context
                ),
            },
        )
        emitted = emit(policy_event)
        if asyncio.iscoroutine(emitted):
            await emitted
        thread_id = await self._resolve_thread(context, conversation)
        self._active_thread_id = thread_id
        turn_settings = await self._turn_settings(context)
        effective_model, effective_effort = await self._effective_settings(
            turn_settings, conversation
        )
        try:
            response = await self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": self._member_broker.prompt(prompt),
                        }
                    ],
                    "cwd": str(context.cwd),
                    "approvalPolicy": _APPROVAL_POLICY,
                    **turn_settings,
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
            model=effective_model,
            effort=effective_effort,
        )

    async def interrupt(self) -> None:
        await self._member_broker.deactivate()
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
        try:
            await self._close_provider()
        finally:
            await self._member_broker.close()

    async def _close_provider(self) -> None:
        """Stop only Codex App Server while preserving the active broker."""
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
        reason = unsupported_network_reason(
            "codex", context.sandbox.network, sys.platform
        )
        if reason:
            raise AgentRuntimeError(AgentRuntimeErrorCategory.CONFIGURATION, reason)
        overrides = _codex_sandbox_overrides(
            context.sandbox,
            Path(resolve_cli_agent_path(self._executable) or self._executable),
            _codex_skill_roots(),
        )
        # The profile is fixed at launch, so a turn under a different contract
        # needs a process of its own rather than the one still running.
        if self._transport.running and overrides == self._sandbox_overrides:
            return
        if self._transport.process is not None:
            await self._close_provider()
        cwd = context.cwd
        self._sandbox_overrides = overrides
        env, self._gh_config_dir = isolated_agent_environment()
        env.update(self._member_broker.provider_environment())
        try:
            process = await create_agent_subprocess(
                self._executable,
                "app-server",
                *_codex_mcp_arguments(self._member_broker),
                *_config_arguments(self._sandbox_overrides),
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
                {"cwd": str(context.cwd), "approvalPolicy": _APPROVAL_POLICY},
            )
        thread_id = _identifier(_dict(_dict(response).get("thread")))
        if not thread_id:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                "Codex returned no thread id.",
                rotate_session=True,
            )
        return thread_id

    async def _turn_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        """Translate the turn's effort decision into ``turn/start`` fields.

        The configured effort mapping is the only source of these values. The
        provider-neutral label is deliberately not used as a fallback: a level
        with no mapping is "no intervention" everywhere else, and Codex keeping
        whatever the last ``turn/start`` carried makes omitting them exactly
        that.
        """
        settings = {
            key: value
            for key, value in context.provider_options.items()
            if key in _TURN_SETTING_KEYS
        }
        unknown = sorted(set(context.provider_options) - _TURN_SETTING_KEYS)
        if unknown:
            _LOGGER.warning(
                "Ignoring unsupported Codex effort settings: %s", ", ".join(unknown)
            )
        return await self._validated_turn_settings(settings)

    async def _effective_settings(
        self, settings: dict[str, Any], conversation: ConversationRecord
    ) -> tuple[str, str]:
        """The model and effort the turn really runs with.

        ``settings`` has already been validated, so what it still names is what
        ``turn/start`` carries. A resumed thread keeps whatever the last
        ``turn/start`` imposed, so an omitted setting falls back to the value
        the conversation last ran with. A thread that never had one imposed
        runs on the catalog's advertised defaults: the entry Codex marks as the
        default model, and the effective model's ``defaultReasoningEffort``.
        """
        model = str(settings.get("model", "") or "")
        effort = str(settings.get("effort", "") or "")
        if conversation.provider_session_id:
            model = model or conversation.effective_model
            effort = effort or conversation.effective_effort
        catalog = await self._models()
        if not model:
            entry = _default_entry(catalog) or {}
            model = str(entry.get("id", "") or "")
        if not effort:
            # An effort this conversation never imposed means every turn ran on
            # the model's own default, which the catalog names per entry.
            effort = str(catalog.get(model, {}).get("defaultReasoningEffort", "") or "")
        return model, effort

    async def _validated_turn_settings(
        self, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop settings the installed Codex reports as unsupported."""
        if not settings:
            return {}
        catalog = await self._models()
        if not catalog:
            return settings
        model_id = str(settings.get("model", "") or "")
        if model_id and model_id not in catalog:
            _LOGGER.warning("Codex does not offer model '%s'; ignoring it.", model_id)
            settings.pop("model")
            model_id = ""
        effort = str(settings.get("effort", "") or "")
        if not effort:
            return settings
        # With no model of our own, the turn runs on the thread's default, so
        # that entry is the one whose efforts have to be checked.
        entry = catalog.get(model_id) if model_id else _default_entry(catalog)
        supported = _supported_efforts(entry) if entry is not None else set()
        if supported and effort not in supported:
            _LOGGER.warning(
                "Codex model '%s' does not support reasoning effort '%s'; "
                "leaving the session's current effort in place.",
                model_id or "(default)",
                effort,
            )
            settings.pop("effort")
        return settings

    async def _models(self) -> dict[str, dict[str, Any]]:
        if self._model_catalog:
            return self._model_catalog
        try:
            response = _dict(await self._request("model/list", {}))
        except RpcError:
            # Older Codex builds do not expose the catalog; skip validation.
            return {}
        # `model/list` returns a paginated envelope: the entries are under
        # `data`, alongside `nextCursor`.
        models = response.get("data")
        if not isinstance(models, list):
            return {}
        self._model_catalog = {
            identifier: entry
            for entry in models
            if isinstance(entry, dict) and (identifier := str(entry.get("id") or ""))
        }
        return self._model_catalog

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


def _codex_mcp_arguments(broker: MemberCapabilityBroker) -> tuple[str, ...]:
    """Build strict per-process MCP overrides for Codex App Server."""
    endpoint = broker.endpoint
    prefix = f"mcp_servers.{endpoint.name}"
    return (
        "-c",
        f"{prefix}.url={json.dumps(endpoint.url)}",
        "-c",
        f'{prefix}.bearer_token_env_var="{MEMBER_BROKER_TOKEN_ENV}"',
        "-c",
        f"{prefix}.required=true",
        "-c",
        f'{prefix}.enabled_tools=["guildbotics_member"]',
        "-c",
        f'{prefix}.tools.guildbotics_member.approval_mode="approve"',
    )


def _codex_skill_roots(home: Path | None = None) -> tuple[Path, ...]:
    """The skill directories Codex scans on this device, of those that exist."""
    home_dir = home or Path.home()
    config_folder = Path(os.environ.get("CODEX_HOME") or home_dir / ".codex")
    candidates = [
        *(home_dir / root for root in _HOME_SKILL_ROOTS),
        *(config_folder / root for root in _CONFIG_SKILL_ROOTS),
    ]
    return tuple(root.resolve() for root in candidates if root.is_dir())


def _codex_sandbox_overrides(
    contract: SandboxContract, executable: Path, skill_roots: tuple[Path, ...] = ()
) -> dict[str, Any]:
    """Translate the sandbox contract into Codex configuration overrides.

    The profile is built from Codex's platform paths (`:minimal`) rather than
    its `:workspace` baseline, whose system-wide read would expose `~/.ssh` and
    `~/.codex/auth.json`. Reads are then the working directory, the directories
    Codex's own binary is run through, Codex's skill roots, and the contract's
    grants (the trees this device's PATH derives, the documents, the local
    paths). The working directory needs no entry of its own: it is the thread's
    workspace root. Its `.git` is granted explicitly because Codex otherwise
    keeps repository metadata read-only under a writable root, and the agent
    stages its own changes with plain git before the broker commits them.

    The contract's denied paths close corners of what is open (`deny`), except
    one that would swallow a directory Codex itself is run through: `~/.codex`
    holds the binary and skills, and `:minimal` already keeps the rest of it,
    `auth.json` included, unreadable.
    """
    profile = f"permissions.{_PERMISSION_PROFILE}"
    filesystem: dict[str, Any] = {
        ":minimal": "read",
        ":workspace_roots": {".": "write", ".git": "write"},
        ":tmpdir": "write",
        ":slash_tmp": "write",
    }
    own = (*executable_read_roots(executable), *skill_roots)
    for root in own:
        filesystem[str(root)] = "read"
    for grant in contract.access.entries():
        filesystem[str(grant.path)] = (
            "write" if grant.access == "read_write" else "read"
        )
    for denied in contract.access.denied:
        if not any(root.is_relative_to(denied.path) for root in own):
            filesystem[str(denied.path)] = "deny"
    overrides: dict[str, Any] = {
        "default_permissions": _PERMISSION_PROFILE,
        f"{profile}.filesystem": filesystem,
    }
    command = contract.network.command
    overrides[f"{profile}.network.enabled"] = command.mode != "deny"
    if command.mode == "allowlist":
        # Domain rules are enforced only through the network proxy; without
        # it `enabled = true` would open every host.
        overrides["features.network_proxy"] = True
        overrides[f"{profile}.network.domains"] = dict.fromkeys(
            command.allowed_domains, "allow"
        )
        overrides[f"{profile}.network.allow_local_binding"] = (
            command.allow_local_network
        )
    web = contract.network.web
    if web.mode == "deny":
        overrides["web_search"] = "disabled"
    elif web.mode == "allowlist":
        overrides["tools.web_search.allowed_domains"] = list(web.allowed_domains)
    return overrides


def _config_arguments(overrides: dict[str, Any]) -> tuple[str, ...]:
    """`-c key=value` pairs; values are TOML so tables and lists survive."""
    arguments: list[str] = []
    for key, value in overrides.items():
        arguments.extend(("-c", f"{key}={_toml(value)}"))
    return tuple(arguments)


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items())
        return "{" + entries + "}"
    raise TypeError(f"Unsupported override value: {value!r}")


def _redacted_overrides(
    overrides: dict[str, Any], context: AgentExecutionContext
) -> dict[str, Any]:
    """The overrides as recorded, with device paths masked."""

    def mask(value: Any) -> Any:
        if isinstance(value, dict):
            return {_mask_key(key): mask(item) for key, item in value.items()}
        return value

    def _mask_key(key: str) -> str:
        if key.startswith("/") or (len(key) > 1 and key[1] == ":"):
            return redact_path(Path(key), workspace_root=context.workspace_data_root)
        return key

    return mask(overrides)


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
