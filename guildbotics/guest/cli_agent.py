import asyncio
import json
import os
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from guildbotics.guest.factory import create_native_adapter
from guildbotics.guest.host_client import (
    ClientConversationStore,
    HostClient,
)
from guildbotics.guest.turn import turn_window
from guildbotics.intelligences.agent_runtime.models import (
    CONTEXT_COMPACTION,
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    CliAgentExecutionError,
    CliAgentExecutionResult,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
    normalize_cli_agent_retry_after,
)
from guildbotics.intelligences.agent_runtime.wire import (
    TURN_WORKING_DIRECTORY,
    CommandFacts,
    CredentialEntry,
    EventEntry,
    HostCallError,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.cli_agents import get_cli_agent_mapping
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.effort import (
    ResolvedEffort,
    effort_diagnostics,
    effort_settings,
    resolve_effort,
)
from guildbotics.runtime.brain import (
    Brain,
    ExecutionMetadata,
    public_parameters,
)
from guildbotics.utils.correlation import current_span, span_scope
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.text_utils import replace_placeholders

#: How many of a turn's records go to the host at a time, and how long one
#: waits at most before it goes.
_RECORD_BATCH = 64
_RECORD_SECONDS = 0.5


#: The one effort-mapping key the core gives a name of its own for diagnostics.
#: Everything else is passed through to the adapter, which owns the provider vocabulary.
EFFORT_MODEL_KEY = "model"


@dataclass(frozen=True)
class EffortDecision:
    """A resolved effort level together with its provider-specific settings.

    ``provider_options`` is everything the turn hands the tool: the tool's own
    baseline settings with the level's overlay merged on top. ``overlay`` is the
    level's own contribution alone — diagnostics are built from it, so an
    unmapped level reads as ``unsupported`` even when a baseline still supplies
    settings, exactly as it does on the LLM API path.
    """

    resolved: ResolvedEffort = field(default_factory=ResolvedEffort)
    overlay: dict[str, Any] = field(default_factory=dict)
    provider_options: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return str(self.provider_options.get(EFFORT_MODEL_KEY, "") or "")

    def diagnostics(self) -> dict[str, Any]:
        return effort_diagnostics(self.resolved, self.overlay, model=self.model)


def _agent_execution_context(kwargs: dict[str, Any]) -> dict[str, Any]:
    raw = (kwargs.get("session_state") or {}).get("agent_execution_context")
    return raw if isinstance(raw, dict) else {}


def _attempt(context: dict[str, Any]) -> int:
    try:
        return max(1, int(context.get("attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _context_is_complete(context: dict[str, Any]) -> bool:
    value = context.get("rebuild_context_complete", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _thread_messages_before_current(context: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(str(context.get("rebuild_context") or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    current_cursor = str(context.get("context_cursor") or "")
    messages = [dict(item) for item in decoded if isinstance(item, dict)]
    if current_cursor:
        messages = [
            message
            for message in messages
            if _cursor_is_before(str(message.get("timestamp") or ""), current_cursor)
        ]
    return messages


def _thread_context_input(
    input: str, context: dict[str, Any], *, mode: str, after_cursor: str = ""
) -> str:
    if mode in {"full", "incremental"}:
        messages = _thread_messages_before_current(context)
        if mode == "incremental":
            messages = [
                item
                for item in messages
                if _cursor_is_before(after_cursor, str(item.get("timestamp") or ""))
            ]
        payload = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            f'<guildbotics_thread_context mode="{mode}">'
            f"{payload}</guildbotics_thread_context>\n\n{input}"
        )
    return f'<guildbotics_thread_context mode="{mode}" />\n\n{input}'


def _continuation_input(input: str, context: dict[str, Any]) -> str:
    return str(context.get("continuation_input") or "").strip() or input


def _continuation_identity_matches(
    context: AgentExecutionContext, conversation: ConversationRecord
) -> bool:
    """A continuation may only target the run/event the session last worked on."""
    if not conversation.last_run_id or conversation.last_run_id != context.run_id:
        return False
    return conversation.last_event_id == context.event_id


def _continuation_rejection(
    context: AgentExecutionContext, conversation: ConversationRecord
) -> dict[str, str] | None:
    """Detect a resumed session that must not continue this turn.

    Returns diagnostics details when the persisted session belongs to a newer
    thread position (cursor regression), an unorderable cursor, or a different
    run/event than the one being retried. The caller rotates the session and
    re-feeds full context instead of sending the generic continuation prompt,
    which would let the agent mistake another run's completion for this one.
    """
    if not conversation.provider_session_id:
        return None
    identity_ok = _continuation_identity_matches(context, conversation)
    if context.conversation_key.work_kind != "chat":
        if context.attempt > 1 and not identity_ok:
            return _rejection_details("identity_mismatch", context, conversation)
        return None
    relation = _cursor_relation(context.context_cursor, conversation.context_cursor)
    if relation == "older":
        return _rejection_details("cursor_regression", context, conversation)
    if relation == "unknown":
        return _rejection_details("cursor_unordered", context, conversation)
    if relation == "equal" and not identity_ok:
        return _rejection_details("identity_mismatch", context, conversation)
    return None


def _rejection_details(
    reason: str, context: AgentExecutionContext, conversation: ConversationRecord
) -> dict[str, str]:
    return {
        "reason": reason,
        "current_cursor": context.context_cursor,
        "persisted_cursor": conversation.context_cursor,
        "event_id": context.event_id,
        "run_id": context.run_id,
        "last_event_id": conversation.last_event_id,
        "last_run_id": conversation.last_run_id,
    }


def _native_turn_input(
    input: str,
    configured: dict[str, Any],
    context: AgentExecutionContext,
    conversation: ConversationRecord,
) -> str:
    """Choose the provider input after continuation safety has been enforced.

    ``_continuation_rejection`` has already rotated any session that must not
    continue, so a surviving session with an ``equal`` cursor (chat) or a
    retried attempt (non-chat) is a legitimate same-run/event continuation.
    """
    if conversation.provider_session_id:
        if context.conversation_key.work_kind != "chat":
            if context.attempt > 1:
                return _continuation_input(input, configured)
            return input
        relation = _cursor_relation(context.context_cursor, conversation.context_cursor)
        if relation == "equal":
            return _thread_context_input(
                _continuation_input(input, configured), configured, mode="continuation"
            )
        return _thread_context_input(
            input,
            configured,
            mode="incremental",
            after_cursor=conversation.context_cursor,
        )
    if context.conversation_key.work_kind == "chat":
        mode = "full" if context.rebuild_context_complete else "inspect_required"
        return _thread_context_input(input, configured, mode=mode)
    return input


def _login_refused(
    context: AgentExecutionContext, report: str
) -> AgentRuntimeError | None:
    """The turn's failure when the login it was lent could not be used,
    whatever the tool made of that; what the tool said goes along with it."""
    refusal = context.login.refusal()
    if not refusal:
        return None
    return AgentRuntimeError(
        AgentRuntimeErrorCategory.AUTHENTICATION, refusal, details={"stderr": report}
    )


class _TurnRecords:
    """What a turn records, sent to the host through the command's window in
    order: a few at a time, so a turn streaming an event per token neither
    makes a call per token nor waits long to be seen. What the host refuses
    to record is logged and dropped: the turn's work is what it did, not the
    record of it."""

    def __init__(self, client: HostClient) -> None:
        self.client = client
        self._entries: list[BaseModel] = []
        self._sent = time.monotonic()

    async def add(self, entry: BaseModel) -> None:
        self._entries.append(entry)
        if (
            len(self._entries) >= _RECORD_BATCH
            or time.monotonic() - self._sent >= _RECORD_SECONDS
        ):
            await self.flush()

    async def flush(self) -> None:
        entries, self._entries = self._entries, []
        self._sent = time.monotonic()
        if entries:
            try:
                await self.client.record(entries)
            except HostCallError as exc:
                get_logger().warning("The host did not record the turn: %s", exc)


class PromptInfo:
    """
    Information about a prompt for an agent.
    """

    def __init__(
        self,
        response_class: type[BaseModel] | None,
        description: str,
    ):
        """
        Initialize the prompt information.

        Args:
            response_class (Type[BaseModel]): The class of the response.
            description (str): A description of the prompt.
        """
        self.response_class = response_class
        self.description = description

    def to_prompt(
        self, user_input: str, session_state: dict, template_engine: str
    ) -> str:
        """Generate a prompt payload in Markdown combining description,
        response schema, and user input.

        Args:
            user_input (str): The user's input instructions.
            session_state (dict): The current session state for placeholder replacement.
            template_engine (str): The template engine to use for placeholder replacement.

        Returns:
            str: A Markdown-formatted prompt ready to send to the AI CLI tool.
        """
        # Create JSON schema for the response model
        description = replace_placeholders(
            self.description, session_state, template_engine
        )

        return to_plain_text(description, user_input, self.response_class)


class CliAgentBrain(Brain):
    """
    Intelligence that runs an AI CLI tool.
    """

    def __init__(self, *args: Any, cli_agent: str = "default", **kwargs: Any):
        """Take the :class:`Brain` arguments, and the AI CLI tool slot to run."""
        super().__init__(*args, **kwargs)
        self.prompt_info = PromptInfo(
            response_class=self.response_class,
            description=self.description,
        )
        self.executable_info = get_cli_agent_mapping(self.person_id)[cli_agent]
        self.cli_agent = cli_agent

    @property
    def configuration(self) -> dict[str, Any]:
        return {
            **super().configuration,
            "slot": self.cli_agent,
            "provider": self.executable_info.adapter,
            "model": str(self.executable_info.parameters.get("model", "")),
            "parameters": public_parameters(self.executable_info.parameters),
        }

    async def run(self, message: str, **kwargs):
        """
        Run the AI CLI tool with the provided arguments.

        Args:
            message (str): The message to pass to the agent.
            **kwargs: Arguments to pass to the agent.
        """
        records = _TurnRecords(turn_window())
        result = await self._run(message, kwargs, records)
        output: Any = result.stdout
        self.execution = ExecutionMetadata(model=result.model, usage=result.usage)
        self._raise_if_execution_failed(result)
        if self.response_class:
            output = to_response_class(output, self.response_class)
        if isinstance(output, AgentResponse):
            trace_id = CommandFacts.read(os.environ).trace_id
            if output.status == AgentResponse.ASKING and trace_id:
                output.message = (
                    f"{output.message}\n\n"
                    f"{t('intelligences.cli_agent.trace_reference', trace_id=trace_id)}"
                )
        return output

    async def run_with_execution_details(
        self, message: str, **kwargs
    ) -> CliAgentExecutionResult:
        return await self._run(message, kwargs, _TurnRecords(turn_window()))

    async def _run(
        self, message: str, kwargs: dict[str, Any], records: _TurnRecords
    ) -> CliAgentExecutionResult:
        """Run the turn in its span, and have the host record it: the request,
        the turn's events, the response, what it proved of the tool's login,
        and how the span ended."""
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        effort = self._resolve_provider_effort(kwargs)
        model = effort.model or str(_agent_execution_context(kwargs).get("model") or "")
        with span_scope("cli_agent"):
            started = time.monotonic()
            await records.add(self._request_entry(input, kwargs, effort))
            try:
                result = await self._execute(input, cwd, kwargs, effort, records, model)
            except BaseException:
                await self._end_span(records, started, "failed", bool(model))
                raise
            await records.add(self._response_entry(result))
            if (credential := self._credential_entry(result)) is not None:
                await records.add(credential)
            await self._end_span(
                records,
                started,
                "finished" if result.returncode == 0 else "failed",
                bool(model),
                result,
            )
        return result

    async def _end_span(
        self,
        records: _TurnRecords,
        started: float,
        status: Literal["finished", "failed"],
        model_specified: bool,
        result: CliAgentExecutionResult | None = None,
    ) -> None:
        """Close the span with what the turn really ran on.

        An unknown model stays empty rather than being papered over with the
        slot name — the span is still attributable through ``agent.slot``, and
        an invented effective value is worse than an absent one. A run that
        never reached the provider has no effective values at all.
        """
        await records.add(
            SummaryEntry(
                span=current_span(),
                slot=self.cli_agent,
                tool=self.executable_info.adapter,
                status=status,
                model_specified=model_specified,
                model=result.model if result else "",
                effort=result.effort if result else "",
                duration_ms=(time.monotonic() - started) * 1000,
                usage=result.usage if result else None,
            )
        )
        await records.flush()

    def _resolve_provider_effort(self, kwargs: dict[str, Any]) -> EffortDecision:
        """Resolve the effort level and translate it into provider settings."""
        resolved = resolve_effort(
            kwargs.get("session_state"), self.effort, logger=self.logger
        )
        overlay = effort_settings(
            self.executable_info.effort, resolved, logger=self.logger
        )
        # The tool's own settings always apply; the level only overlays them, so
        # a slot can name a model without tying it to an effort level.
        options = {**deepcopy(self.executable_info.parameters), **overlay}
        return EffortDecision(
            resolved=resolved, overlay=overlay, provider_options=options
        )

    async def _execute(
        self,
        input: str,
        cwd: Path | str,
        kwargs: dict[str, Any],
        effort: EffortDecision,
        records: _TurnRecords,
        model: str,
    ) -> CliAgentExecutionResult:
        """Run the turn for the command's run and work, which the host's grant
        holds every turn of the command to, and which give it the execution
        lease the run holds and its conversation."""
        facts = CommandFacts.read(os.environ)
        configured = _agent_execution_context(kwargs)
        adapter_name = self.executable_info.adapter
        key = ConversationKey(
            person_id=self.person_id,
            adapter=adapter_name,
            work_kind=facts.work_kind,
            work_identity=facts.work_identity,
        )
        try:
            policy = ResumePolicy(str(configured.get("resume_policy") or "fresh"))
        except ValueError:
            policy = ResumePolicy.FRESH
        context = AgentExecutionContext(
            person_id=self.person_id,
            run_id=facts.run_id,
            cwd=Path(cwd),
            conversation_key=key,
            trace_id=facts.trace_id,
            resume_policy=policy,
            context_cursor=str(configured.get("context_cursor") or ""),
            event_id=str(configured.get("event_id") or ""),
            model=model,
            # `default` and unspecified request no effort overlay or rotation.
            # Whether resumed settings persist depends on the adapter and provider.
            # A level whose overlay is empty also imposed nothing of its
            # own, so it must not be reported as the turn's effort.
            effort=(
                effort.resolved.resolved
                if effort.resolved.intervenes and effort.overlay
                else ""
            ),
            provider_options=dict(effort.provider_options),
            rebuild_context=str(configured.get("rebuild_context") or ""),
            rebuild_context_complete=_context_is_complete(configured),
            attempt=_attempt(configured),
            continuation_input=str(configured.get("continuation_input") or ""),
            participant_labels=str(configured.get("participant_labels") or ""),
        )
        return await self._execute_native_turn(
            input=input,
            configured=configured,
            context=context,
            adapter_name=adapter_name,
            records=records,
        )

    async def _execute_native_turn(
        self,
        *,
        input: str,
        configured: dict[str, Any],
        context: AgentExecutionContext,
        adapter_name: str,
        records: _TurnRecords,
    ) -> CliAgentExecutionResult:
        store = ClientConversationStore(records.client)
        # The adapter is the turn's: its provider starts with the turn and
        # ends with it, and a session outlives it by being resumed by id.
        adapter = create_native_adapter(adapter_name)
        try:
            try:
                conversation = store.resolve(
                    context.conversation_key,
                    context.resume_policy,
                    model=context.model,
                )
            except LookupError as exc:
                return CliAgentExecutionResult(
                    stdout="",
                    stderr=str(exc),
                    returncode=1,
                    error_category="session_unavailable",
                )

            async def emit(event: Any) -> None:
                await records.add(
                    EventEntry(
                        span=current_span(),
                        conversation=conversation.key,
                        generation=conversation.generation,
                        context_cursor=context.context_cursor,
                        event=event,
                    )
                )

            try:
                rejection = _continuation_rejection(context, conversation)
                if rejection is not None:
                    await emit(
                        AgentEvent(
                            AgentEventKind.TURN,
                            "continuation_rejected",
                            message=(
                                "resumed session cannot safely continue this turn; "
                                "rotating to a fresh session with full context"
                            ),
                            provider_session_id=conversation.provider_session_id,
                            details=rejection,
                        )
                    )
                    conversation.rotate(str(rejection["reason"]))
                native_input = _native_turn_input(
                    input, configured, context, conversation
                )
                await emit(
                    AgentEvent(
                        AgentEventKind.TURN,
                        "started",
                        provider_session_id=conversation.provider_session_id,
                        details={
                            "work_kind": context.conversation_key.work_kind,
                            # Where the turn works; the host records what the
                            # environment confines it to there, whatever provider
                            # runs it.
                            TURN_WORKING_DIRECTORY: str(context.cwd),
                        },
                    )
                )
                try:
                    terminal = await adapter.run_turn(
                        native_input, context, conversation, emit
                    )
                except Exception as exc:
                    if refused := _login_refused(context, str(exc)):
                        raise refused from exc
                    raise
                if refused := _login_refused(context, terminal.output):
                    raise refused
            except asyncio.CancelledError:
                store.mark_unhealthy(conversation, "cancelled")
                raise
            except AgentRuntimeError as exc:
                await emit(
                    AgentEvent(
                        AgentEventKind.FAILED,
                        exc.category.value,
                        message=str(exc),
                        provider_session_id=conversation.provider_session_id,
                        details=exc.details,
                    )
                )
                if exc.rotate_session:
                    store.mark_unhealthy(conversation, exc.category.value)
                details = {str(key): str(value) for key, value in exc.details.items()}
                details["cli_agent"] = adapter_name
                if exc.category is AgentRuntimeErrorCategory.RATE_LIMITED:
                    _normalize_native_retry_after(details)
                # What the tool itself said last is the only lead a reader has
                # when the process just ended, so it rides along with the reason.
                stderr = str(exc)
                if tail := details.get("stderr", "").strip():
                    stderr = f"{stderr}\n{tail}"
                return CliAgentExecutionResult(
                    stdout="",
                    stderr=stderr,
                    returncode=1,
                    error_category=exc.category.value,
                    error_details=details,
                    provider_session_id=conversation.provider_session_id,
                )
            conversation.provider_session_id = terminal.provider_session_id
            conversation.provider_turn_id = terminal.provider_turn_id
            conversation.provider = adapter_name
            # Retain the last known values when the turn reports none. Each
            # adapter decides whether resume needs those values sent again.
            conversation.effective_model = (
                terminal.model or conversation.effective_model
            )
            conversation.effective_effort = (
                terminal.effort or conversation.effective_effort
            )
            # The cursor is a monotonic watermark of what was fed into the provider
            # session; never let a re-dispatched older event rewind it.
            if not conversation.context_cursor or (
                _cursor_relation(context.context_cursor, conversation.context_cursor)
                == "newer"
            ):
                conversation.context_cursor = context.context_cursor
            conversation.last_event_id = context.event_id
            conversation.last_run_id = context.run_id
            conversation.turn_count += 1
            conversation.input_tokens += terminal.usage.get("input_tokens", 0)
            conversation.output_tokens += terminal.usage.get("output_tokens", 0)
            # Context usage is the provider's absolute session size, so the latest
            # snapshot replaces the stored one instead of being summed with it.
            if "context_size_tokens" in terminal.usage:
                conversation.context_used_tokens = terminal.usage.get(
                    "context_used_tokens", 0
                )
                conversation.context_size_tokens = terminal.usage["context_size_tokens"]
            compacted = any(
                event.kind is AgentEventKind.TURN and event.name == CONTEXT_COMPACTION
                for event in terminal.events
            )
            conversation.healthy = not compacted
            if compacted:
                conversation.rotation_reason = CONTEXT_COMPACTION
            store.save(conversation)
            return CliAgentExecutionResult(
                stdout=terminal.output.strip(),
                stderr=terminal.stderr.strip(),
                returncode=terminal.returncode,
                provider_session_id=terminal.provider_session_id,
                provider_turn_id=terminal.provider_turn_id,
                finish_reason=terminal.finish_reason,
                usage=dict(terminal.usage),
                model=terminal.model,
                effort=terminal.effort,
            )
        finally:
            await adapter.close()

    def _credential_entry(
        self, result: CliAgentExecutionResult
    ) -> CredentialEntry | None:
        """What the turn proved about the tool's login here, if anything:
        other failures say nothing about credentials."""
        if result.error_category == "authentication":
            failed = True
        elif result.error_category or result.returncode != 0:
            return None
        else:
            failed = False
        return CredentialEntry(
            span=current_span(), tool=self._agent_name(result), failed=failed
        )

    def _agent_name(self, result: CliAgentExecutionResult) -> str:
        return (
            result.error_details.get("cli_agent")
            or self.executable_info.adapter
            or self.cli_agent
        )

    def _raise_if_execution_failed(self, result: CliAgentExecutionResult) -> None:
        if result.error_category in {"authentication", "rate_limited"}:
            raise CliAgentExecutionError(
                cli_agent=self._agent_name(result), result=result
            )
        if result.returncode != 0:
            raise CliAgentExecutionError(cli_agent=self.cli_agent, result=result)
        if not result.stdout:
            detail = result.stderr or "no output"
            raise CliAgentExecutionError(
                cli_agent=self.cli_agent,
                result=result,
                message=f"AI CLI tool '{self.cli_agent}' produced no response: {detail}",
            )

    def _request_entry(
        self, prompt: str, kwargs: dict[str, Any], effort: EffortDecision
    ) -> IoEntry:
        return IoEntry(
            span=current_span(),
            io_type="cli_agent.request",
            payload={
                "effort": effort.diagnostics(),
                "person_id": self.person_id,
                "brain": self.name,
                "cli_agent": self.cli_agent,
                "cwd": str(kwargs.get("cwd")),
                "response_class": (
                    self.response_class.__name__ if self.response_class else ""
                ),
                "prompt": prompt,
            },
        )

    def _response_entry(self, result: CliAgentExecutionResult) -> IoEntry:
        # The whole stderr goes: the host keeps what it records of it.
        return IoEntry(
            span=current_span(),
            io_type="cli_agent.response",
            payload={
                "person_id": self.person_id,
                "brain": self.name,
                "cli_agent": self.cli_agent,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )


def _normalize_native_retry_after(details: dict[str, str]) -> None:
    if details.get("retry_after_at"):
        return
    retry_after_at = normalize_cli_agent_retry_after(
        details.get("retry_after_text", ""),
        details.get("retry_after_timezone", ""),
    )
    if retry_after_at:
        details["retry_after_at"] = retry_after_at
        return
    try:
        seconds = float(details.get("retry_after_seconds", "0") or 0)
    except ValueError:
        return
    if seconds > 0:
        details["retry_after_at"] = (
            datetime.now().astimezone() + timedelta(seconds=seconds)
        ).isoformat(timespec="seconds")


def _cursor_relation(current: str, persisted: str) -> str:
    """Relate a turn's context cursor to the persisted session watermark.

    Cursors are chat message positions, which order as text. Returns
    ``newer`` / ``equal`` / ``older``, or ``unknown`` when either side is
    missing, which must never be treated as a continuation.
    """
    if not current or not persisted:
        return "unknown"
    if current > persisted:
        return "newer"
    return "equal" if current == persisted else "older"


def _cursor_is_before(candidate: str, current: str) -> bool:
    return bool(candidate and current) and candidate < current
