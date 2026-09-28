import asyncio
import json
import os
import re
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from guildbotics.intelligences.agent_runtime.factory import create_native_adapter
from guildbotics.intelligences.agent_runtime.host_client import (
    TURN_WORKING_DIRECTORY,
    ClientConversationStore,
    CommandFacts,
    CredentialEntry,
    EventEntry,
    HostCallError,
    HostClient,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.agent_runtime.models import (
    CONTEXT_COMPACTION,
    SETTINGS_SCOPE_SESSION,
    SETTINGS_SCOPE_TURN,
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
    settings_fingerprint,
)
from guildbotics.intelligences.agent_runtime.turn import turn_window
from guildbotics.intelligences.brains.brain import (
    Brain,
    ExecutionMetadata,
    public_parameters,
)
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.effort import (
    ResolvedEffort,
    effort_diagnostics,
    effort_settings,
    resolve_effort,
    validate_effort_overlay,
)
from guildbotics.observability import current_span, span_scope
from guildbotics.utils.fileio import (
    get_person_config_path,
    load_person_slot_mapping,
    load_yaml_file,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.text_utils import replace_placeholders

#: How many of a turn's records go to the host at a time, and how long one
#: waits at most before it goes.
_RECORD_BATCH = 64
_RECORD_SECONDS = 0.5
_HOURS_PER_HALF_DAY = 12
_MAX_24_HOUR = 23
_MAX_MINUTE = 59
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class ExecutableInfo:
    """The AI CLI tool a brain slot runs, and the settings it runs it with.

    ``adapter`` is the tool's catalog name, which is also the name of the native
    adapter that drives it. ``effort`` maps an effort level to provider-specific
    settings; only the common ``model`` key is understood by the core, every
    other key being validated by the adapter. ``parameters`` are the settings
    that always apply, whatever effort was asked for, with the effort overlay
    merged on top -- the same way a model definition's ``parameters`` relate to
    its ``effort``.
    """

    adapter: str = ""
    effort: dict[str, dict] = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)


#: Overrides keyed by person id. Production code does not write this dict.
#: See ``simple_brain_factory.person_brain_mapping`` for why.
person_cli_agent_mapping: dict[str, dict[str, ExecutableInfo]] = {}

#: The one effort-mapping key the core gives a name of its own, because it feeds
#: the settings fingerprint. Everything else is passed through to the adapter,
#: which owns the rest of the provider vocabulary.
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


@dataclass(frozen=True)
class CliAgentExecutionResult:
    stdout: str
    stderr: str
    returncode: int
    error_category: str = ""
    error_details: dict[str, str] = field(default_factory=dict)
    provider_session_id: str = ""
    provider_turn_id: str = ""
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    #: What the turn really ran with, as reported by the adapter. Both are empty
    #: when the provider names neither and the adapter imposed neither.
    model: str = ""
    effort: str = ""


class CliAgentExecutionError(RuntimeError):
    """A turn of an AI CLI tool that produced no usable response.

    The message names the tool and carries the reason as the runtime stated
    it. It claims no exit code: the brain never observes the tool's process,
    only the adapter does, and an adapter that saw one exit puts the code in
    its own words. Most failures reach here without any process at all (the
    device refused the turn, the tool is not logged in, the session is gone),
    so ``returncode`` is only the failed / finished distinction.
    """

    def __init__(
        self,
        *,
        cli_agent: str,
        result: CliAgentExecutionResult,
        message: str | None = None,
    ) -> None:
        self.cli_agent = cli_agent
        self.result = result
        self.category = result.error_category
        self.details = dict(result.error_details)
        detail = result.stderr or result.stdout or "no output"
        super().__init__(message or f"AI CLI tool '{cli_agent}' failed: {detail}")


def normalize_cli_agent_retry_after(
    retry_after_text: str = "",
    retry_after_timezone: str = "",
) -> str:
    text = retry_after_text.strip()
    timezone_text = retry_after_timezone.strip()
    if not text:
        return ""
    timezone = _zoneinfo_or_local(timezone_text)
    relative = _parse_relative_retry_delta(text)
    if relative is not None:
        return (datetime.now().astimezone() + relative).isoformat(timespec="seconds")
    parsed_datetime = _parse_retry_datetime(text, timezone)
    if parsed_datetime is not None:
        return parsed_datetime.isoformat(timespec="seconds")
    parsed_time = _parse_retry_time(text)
    if parsed_time is None:
        return ""
    hour, minute = parsed_time
    now = datetime.now(timezone)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


def _zoneinfo_or_local(timezone_text: str) -> Any:
    if timezone_text:
        with suppress(ZoneInfoNotFoundError):
            return ZoneInfo(timezone_text)
    return datetime.now().astimezone().tzinfo


def _parse_relative_retry_delta(text: str) -> timedelta | None:
    normalized = text.strip()
    if not re.search(r"\b(?:please wait|resets in)\b", normalized, re.IGNORECASE):
        return None
    matches = list(
        re.finditer(
            r"(?P<value>\d+)\s*"
            r"(?P<unit>second|seconds|minute|minutes|hour|hours|s|m|h)\b",
            normalized,
            re.IGNORECASE,
        )
    )
    if not matches:
        return None
    seconds = 0
    for match in matches:
        value = int(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("s"):
            seconds += value
        elif unit.startswith("m"):
            seconds += value * 60
        else:
            seconds += value * 60 * 60
    return timedelta(seconds=seconds)


def _parse_retry_datetime(text: str, timezone: Any) -> datetime | None:
    match = re.search(
        r"(?:try again at|reset on)\s+"
        r"(?P<month>[A-Za-z]+)\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+"
        r"(?P<year>\d{4})\s+(?:at\s+)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
        r"(?P<ampm>am|pm)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    parsed_time = _parse_retry_time(
        f"{match.group('hour')}:{match.group('minute')} {match.group('ampm')}"
    )
    if parsed_time is None:
        return None
    hour, minute = parsed_time
    try:
        return datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            hour,
            minute,
            tzinfo=timezone,
        )
    except ValueError:
        return None


def _parse_retry_time(text: str) -> tuple[int, int] | None:
    match = re.search(
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>am|pm)?",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if hour == _HOURS_PER_HALF_DAY:
            hour = 0
        if ampm == "pm":
            hour += _HOURS_PER_HALF_DAY
    if not 0 <= hour <= _MAX_24_HOUR or not 0 <= minute <= _MAX_MINUTE:
        return None
    return hour, minute


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
    if str(context.get("work_kind") or "") != "chat":
        return input
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
    continuation = str(context.get("continuation_input") or "").strip() or input
    return _thread_context_input(continuation, context, mode="continuation")


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
            return _continuation_input(input, configured)
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


def _cli_agent_definition(person_id: str, definition_path: str) -> dict:
    """Read a definition, filling absent keys from its tool's own default.

    Slots live at ``cli_agents/<tool>/<slot>.yml`` beside the tool's
    ``default.yml``, the same shape model definitions use, so a slot states only
    what it changes and inherits the rest.
    """
    from guildbotics.intelligences.cli_agents import cli_agent_default_path

    data = _yaml_dict(
        get_person_config_path(person_id, f"intelligences/{definition_path}")
    )
    tool_default = cli_agent_default_path(_tool_of(definition_path))
    if tool_default != definition_path:
        inherited = _yaml_dict(
            get_person_config_path(person_id, f"intelligences/{tool_default}")
        )
        data = {**inherited, **data}
    return data


def _yaml_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    data = load_yaml_file(path)
    return data if isinstance(data, dict) else {}


def _parameters_of(definition: dict) -> dict:
    parameters = definition.get("parameters")
    return dict(parameters) if isinstance(parameters, dict) else {}


def _tool_of(definition_path: str) -> str:
    from guildbotics.intelligences.cli_agents import cli_agent_name_from_path

    return cli_agent_name_from_path(definition_path)


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


def get_cli_agent_mapping(person_id: str) -> dict[str, ExecutableInfo]:
    """Return the person's AI CLI slots, read from configuration each call.

    An entry in :data:`person_cli_agent_mapping` is an override and wins over
    the files. Otherwise the mapping is loaded and not stored, so the next
    brain sees a file that changed after the previous brain was built.

    Args:
        person_id (str): The person whose ``cli_agent_mapping.yml`` to read.

    Returns:
        dict[str, ExecutableInfo]: Slot name to the tool and its settings.
    """
    override = person_cli_agent_mapping.get(person_id)
    if override is not None:
        return override

    from guildbotics.intelligences.cli_agents import require_cli_agent_path

    mapping = load_person_slot_mapping(person_id, "intelligences/cli_agent_mapping.yml")
    cli_agent_mapping = {}
    for slot, definition_path in mapping.items():
        path = str(definition_path)
        adapter = require_cli_agent_path(path, where=f"AI CLI tool slot '{slot}'")
        definition = _cli_agent_definition(person_id, path)
        cli_agent_mapping[slot] = ExecutableInfo(
            adapter=adapter,
            effort=validate_effort_overlay(
                definition.get("effort"), where=f"AI CLI tool '{slot}'"
            ),
            parameters=_parameters_of(definition),
        )
    return cli_agent_mapping


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
        with span_scope("cli_agent"):
            started = time.monotonic()
            await records.add(self._request_entry(input, kwargs, effort))
            try:
                result = await self._execute(input, cwd, kwargs, effort, records)
            except BaseException:
                await self._end_span(records, started, "failed")
                raise
            await records.add(self._response_entry(result))
            if (credential := self._credential_entry(result)) is not None:
                await records.add(credential)
            await self._end_span(
                records,
                started,
                "finished" if result.returncode == 0 else "failed",
                result,
            )
        return result

    async def _end_span(
        self,
        records: _TurnRecords,
        started: float,
        status: Literal["finished", "failed"],
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
                status=status,
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
    ) -> CliAgentExecutionResult:
        """Run the turn for the command's run, or the workflow run's work the
        call names: the host's grant holds every turn of the command to its
        run, and gives it the execution lease the run holds."""
        facts = CommandFacts.read(os.environ)
        configured = _agent_execution_context(kwargs)
        adapter_name = self.executable_info.adapter
        run_id = str(configured.get("run_id") or facts.run_id)
        work_kind = str(configured.get("work_kind") or facts.work_kind or "manual")
        key = ConversationKey(
            person_id=self.person_id,
            adapter=adapter_name,
            work_kind=work_kind,
            work_identity=str(configured.get("work_identity") or run_id),
        )
        try:
            policy = ResumePolicy(str(configured.get("resume_policy") or "fresh"))
        except ValueError:
            policy = ResumePolicy.FRESH
        context = AgentExecutionContext(
            person_id=self.person_id,
            run_id=run_id,
            cwd=Path(cwd),
            conversation_key=key,
            trace_id=facts.trace_id,
            resume_policy=policy,
            context_cursor=str(configured.get("context_cursor") or ""),
            event_id=str(configured.get("event_id") or ""),
            model=effort.model or str(configured.get("model") or ""),
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
            # A turn-scoped adapter re-sends its settings on every turn, so a
            # change never justifies discarding the session. For a
            # session-scoped one the fingerprint comes from what the adapter
            # will really impose, so a request it cannot act on does not read
            # as a change.
            fingerprint = (
                ""
                if getattr(adapter, "settings_scope", SETTINGS_SCOPE_SESSION)
                == SETTINGS_SCOPE_TURN
                else settings_fingerprint(adapter.applied_settings(context))
            )
            try:
                conversation = store.resolve(
                    context.conversation_key,
                    context.resume_policy,
                    model=context.model,
                    settings_fingerprint=fingerprint,
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
            # Retain the last known values when the turn reports none. Claude
            # re-sends the recorded model on resume; effort is only carried
            # forward here for reporting when no new value is available.
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

    Returns ``newer`` / ``equal`` / ``older`` for orderable cursors. Cursors
    that cannot be ordered safely (either side missing, or non-numeric and not
    identical) are ``unknown`` and must never be treated as a continuation.
    """
    if not current or not persisted:
        return "unknown"
    try:
        current_parts = tuple(int(part) for part in current.split("."))
        persisted_parts = tuple(int(part) for part in persisted.split("."))
    except ValueError:
        return "equal" if current == persisted else "unknown"
    if current_parts > persisted_parts:
        return "newer"
    if current_parts == persisted_parts:
        return "equal"
    return "older"


def _cursor_is_before(candidate: str, current: str) -> bool:
    if not candidate or not current:
        return False
    try:
        candidate_parts = tuple(int(part) for part in candidate.split("."))
        current_parts = tuple(int(part) for part in current.split("."))
    except ValueError:
        return candidate != current
    return candidate_parts < current_parts
