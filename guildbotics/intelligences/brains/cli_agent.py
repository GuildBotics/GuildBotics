import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging import Logger
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationRecord,
)
from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.common import AgentResponse
from guildbotics.observability import correlation_fields, span_scope
from guildbotics.observability.diagnostics_events import (
    record_correlated_event,
    record_correlated_io,
    record_span_summary,
)
from guildbotics.observability.session_transcripts import (
    standard_stderr_tail,
    transcript_detail,
)
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import get_person_config_path, load_yaml_file
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.text_utils import replace_placeholders
from guildbotics.utils.workspace_state import GUILDBOTICS_CONFIG_DIR

CLI_AGENT_ERROR_MARKER = "GUILDBOTICS_CLI_AGENT_ERROR_JSON:"
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


class ExecutableInfo:
    """
    Information about an executable script.
    """

    def __init__(
        self,
        script: str = "",
        env: dict[str, str] | None = None,
        adapter: str = "",
        conversation_scope: str = "none",
        agent_name: str = "",
    ):
        """
        Initialize the executable information.

        Args:
            script (str): The script to execute.
            env (dict): Environment variables to set for the script.
        """
        self.script = script
        self.env = {} if env is None else env
        self.adapter = adapter
        self.conversation_scope = conversation_scope
        self.agent_name = agent_name


person_cli_agent_mapping: dict[str, dict[str, ExecutableInfo]] = {}


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


class CliAgentExecutionError(RuntimeError):
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
        super().__init__(
            message
            or f"AI CLI tool '{cli_agent}' exited with code {result.returncode}: {detail}"
        )


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


def _parse_cli_agent_error_marker(
    stderr: str, logger: Logger | None = None
) -> tuple[str, dict[str, str]]:
    for line in stderr.splitlines():
        if CLI_AGENT_ERROR_MARKER not in line:
            continue
        _, raw = line.split(CLI_AGENT_ERROR_MARKER, 1)
        try:
            parsed = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            if logger is not None:
                with suppress(Exception):
                    logger.warning("failed to parse AI CLI tool error marker: %s", exc)
            return "", {}
        if not isinstance(parsed, dict):
            return "", {}
        category = str(parsed.get("category", "") or "")
        details = {
            str(key): str(value)
            for key, value in parsed.items()
            if str(key) != "category" and value is not None
        }
        if category == "rate_limited" and not details.get("retry_after_at"):
            retry_after_at = normalize_cli_agent_retry_after(
                details.get("retry_after_text", ""),
                details.get("retry_after_timezone", ""),
            )
            if retry_after_at:
                details["retry_after_at"] = retry_after_at
        return category, details
    return "", {}


def _extra_env(kwargs: dict[str, Any]) -> dict[str, str]:
    """Build the per-invocation environment for one-shot script adapters.

    Workflows pass the provider-neutral execution context; nothing here
    mutates process-global environment variables.
    """
    context = (kwargs.get("session_state") or {}).get("agent_execution_context")
    if not isinstance(context, dict):
        return {}
    from guildbotics.capabilities.completion_retry import (
        CLI_AGENT_CONVERSATION_FILE_ENV,
    )
    from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
    from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR

    run_id = str(context.get("run_id") or "")
    data_root = str(context.get("workspace_data_root") or "")
    work_kind = str(context.get("work_kind") or "")
    result = {
        GUILDBOTICS_DATA_DIR: data_root,
        RUN_ENV if work_kind == "chat" else TASK_RUN_ENV: run_id,
    }
    participant_labels = str(context.get("participant_labels") or "")
    if participant_labels:
        result["GUILDBOTICS_CHAT_PARTICIPANT_LABELS"] = participant_labels
    if run_id and data_root:
        conversation_file = Path(data_root) / "task-runs" / f"{run_id}.agy-conversation"
        conversation_file.parent.mkdir(parents=True, exist_ok=True)
        result[CLI_AGENT_CONVERSATION_FILE_ENV] = str(conversation_file)
    return {key: value for key, value in result.items() if value}


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


def _thread_context_input(input: str, context: dict[str, Any], *, mode: str) -> str:
    if str(context.get("work_kind") or "") != "chat":
        return input
    if mode == "full":
        payload = json.dumps(
            _thread_messages_before_current(context),
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            '<guildbotics_thread_context mode="full">'
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
        return _thread_context_input(input, configured, mode="incremental")
    if context.conversation_key.work_kind == "chat":
        mode = "full" if context.rebuild_context_complete else "inspect_required"
        return _thread_context_input(input, configured, mode=mode)
    return input


def _one_shot_input(
    input: str,
    context: dict[str, Any],
    *,
    conversation_scope: str,
    extra_env: dict[str, str],
) -> str:
    from guildbotics.capabilities.completion_retry import (
        CLI_AGENT_CONVERSATION_FILE_ENV,
    )

    conversation_file = Path(extra_env.get(CLI_AGENT_CONVERSATION_FILE_ENV, ""))
    has_exact_session = False
    if conversation_scope == "dispatch" and _attempt(context) > 1:
        with suppress(OSError):
            has_exact_session = bool(
                conversation_file.read_text(encoding="utf-8").strip()
            )
    if has_exact_session:
        return _continuation_input(input, context)
    mode = "full" if _context_is_complete(context) else "inspect_required"
    return _thread_context_input(input, context, mode=mode)


def _propagate_cwd_workspace_environment(env: dict[str, str]) -> None:
    if not env.get(GUILDBOTICS_CONFIG_DIR, "").strip():
        config_dir = Path.cwd() / ".guildbotics" / "config"
        if config_dir.exists():
            env[GUILDBOTICS_CONFIG_DIR] = str(config_dir.resolve())

    if not env.get(GUILDBOTICS_ENV_FILE, "").strip():
        env_file = Path.cwd() / ".env"
        if env_file.is_file():
            env[GUILDBOTICS_ENV_FILE] = str(env_file.resolve())


def get_cli_agent_mapping(person_id: str) -> dict[str, ExecutableInfo]:
    if person_id in person_cli_agent_mapping:
        return person_cli_agent_mapping[person_id]

    config_file = get_person_config_path(
        person_id, "intelligences/cli_agent_mapping.yml"
    )
    mapping = cast(dict, load_yaml_file(config_file))
    from guildbotics.intelligences.cli_agents import native_cli_agent_name

    cli_agent_mapping = {}
    for name, executable_info_file in mapping.items():
        native_name = native_cli_agent_name(str(executable_info_file))
        if native_name:
            cli_agent_mapping[name] = ExecutableInfo(
                adapter=native_name, agent_name=native_name
            )
            continue
        executable_info_path = get_person_config_path(
            person_id, f"intelligences/cli_agents/{executable_info_file}"
        )
        executable_info = cast(dict, load_yaml_file(executable_info_path))
        cli_agent_mapping[name] = ExecutableInfo(
            script=executable_info.get("script", ""),
            env=executable_info.get("env", {}),
            conversation_scope=str(
                executable_info.get("conversation_scope", "none") or "none"
            ),
            agent_name=str(executable_info_file)
            .removesuffix(".yml")
            .removesuffix("-cli"),
        )
    person_cli_agent_mapping[person_id] = cli_agent_mapping
    return cli_agent_mapping


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

    def __init__(
        self,
        person_id: str,
        name: str,
        logger: Logger,
        description: str = "",
        template_engine: str = "default",
        response_class: type[BaseModel] | None = None,
        cli_agent: str = "default",
    ):
        super().__init__(
            person_id=person_id,
            name=name,
            logger=logger,
            description=description,
            template_engine=template_engine,
            response_class=response_class,
        )

        self.prompt_info = PromptInfo(
            response_class=response_class,
            description=description,
        )

        cli_agent_mapping = get_cli_agent_mapping(person_id)
        self.executable_info = cli_agent_mapping[cli_agent]
        self.logger = logger
        self.cli_agent = cli_agent

    async def run(self, message: str, **kwargs):
        """
        Run the AI CLI tool with the provided arguments.

        Args:
            message (str): The message to pass to the agent.
            **kwargs: Arguments to pass to the agent.
        """
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        # The span wraps the whole call (including this brain's own logging) so
        # logs emitted here are attributed to the "cli_agent" span in diagnostics.
        with span_scope("cli_agent"):
            started = time.monotonic()
            self._write_request_io(input, kwargs)
            try:
                result = await self._execute(input, cwd, kwargs)
            except Exception:
                record_span_summary(
                    status="failed",
                    model=self.cli_agent,
                    duration_ms=(time.monotonic() - started) * 1000,
                    attributes={"agent.kind": "cli_agent"},
                )
                raise
            output: Any = result.stdout
            self._write_response_io(result)
            record_span_summary(
                status="finished" if result.returncode == 0 else "failed",
                model=self.cli_agent,
                duration_ms=(time.monotonic() - started) * 1000,
                usage=result.usage,
                attributes={"agent.kind": "cli_agent"},
            )
            self._raise_if_execution_failed(result)

            if self.response_class:
                output = to_response_class(output, self.response_class)
            if isinstance(output, AgentResponse):
                trace_id = str(correlation_fields().get("trace_id") or "")
                if output.status == AgentResponse.ASKING and trace_id:
                    output.message = (
                        f"{output.message}\n\n"
                        f"{t('intelligences.cli_agent.trace_reference', trace_id=trace_id)}"
                    )

        return output

    async def run_with_execution_details(
        self, message: str, **kwargs
    ) -> CliAgentExecutionResult:
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        with span_scope("cli_agent"):
            started = time.monotonic()
            self._write_request_io(input, kwargs)
            try:
                result = await self._execute(input, cwd, kwargs)
            except Exception:
                record_span_summary(
                    status="failed",
                    model=self.cli_agent,
                    duration_ms=(time.monotonic() - started) * 1000,
                    attributes={"agent.kind": "cli_agent"},
                )
                raise
            self._write_response_io(result)
            record_span_summary(
                status="finished" if result.returncode == 0 else "failed",
                model=self.cli_agent,
                duration_ms=(time.monotonic() - started) * 1000,
                usage=result.usage,
                attributes={"agent.kind": "cli_agent"},
            )
        return result

    async def _execute(
        self,
        input: str,
        cwd: Path | str,
        kwargs: dict[str, Any],
    ) -> CliAgentExecutionResult:
        if self.executable_info.adapter:
            return await self._execute_native(input, cwd, kwargs)
        extra_env = _extra_env(kwargs)
        script_input = _one_shot_input(
            input,
            _agent_execution_context(kwargs),
            conversation_scope=self.executable_info.conversation_scope,
            extra_env=extra_env,
        )
        return await self._execute_script(script_input, cwd, extra_env)

    async def _execute_native(
        self, input: str, cwd: Path | str, kwargs: dict[str, Any]
    ) -> CliAgentExecutionResult:
        from guildbotics.intelligences.agent_runtime.models import (
            ConversationKey,
            ResumePolicy,
        )
        from guildbotics.observability import correlation_fields
        from guildbotics.runtime.person_lease import (
            PersonExecutionLease,
            PersonLeaseUnavailableError,
            current_person_lease,
        )
        from guildbotics.utils.fileio import get_workspace_data_root

        configured = _agent_execution_context(kwargs)
        adapter_name = self.executable_info.adapter
        run_id = str(
            configured.get("run_id")
            or correlation_fields().get("trace_id")
            or uuid4().hex
        )
        data_root = Path(
            str(configured.get("workspace_data_root") or get_workspace_data_root())
        )
        work_kind = str(configured.get("work_kind") or "manual")
        work_identity = str(configured.get("work_identity") or run_id)
        key = ConversationKey(
            person_id=self.person_id,
            adapter=adapter_name,
            work_kind=work_kind,
            work_identity=work_identity,
        )
        try:
            policy = ResumePolicy(str(configured.get("resume_policy") or "fresh"))
        except ValueError:
            policy = ResumePolicy.FRESH
        lease = current_person_lease()
        owned_lease: PersonExecutionLease | None = None
        if lease is None:
            owned_lease = PersonExecutionLease(self.person_id, data_root)
            try:
                owned_lease.acquire(
                    source="manual",
                    command=f"agent:{adapter_name}",
                    work_id=run_id,
                )
            except PersonLeaseUnavailableError as exc:
                return CliAgentExecutionResult(
                    stdout="",
                    stderr=str(exc),
                    returncode=1,
                    error_category="lease_unavailable",
                    error_details={"cli_agent": adapter_name},
                )
            lease = owned_lease
        try:
            lease_metadata = lease.bind_run_id(run_id)
            context = AgentExecutionContext(
                person_id=self.person_id,
                run_id=run_id,
                cwd=Path(cwd),
                workspace_data_root=data_root,
                conversation_key=key,
                resume_policy=policy,
                context_cursor=str(configured.get("context_cursor") or ""),
                event_id=str(configured.get("event_id") or ""),
                lease_id=lease_metadata.lease_id,
                delegation_id=lease_metadata.delegation_id,
                model=str(configured.get("model") or ""),
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
                run_id=run_id,
            )
        finally:
            lease.unbind_run_id(run_id)
            if owned_lease is not None:
                owned_lease.release()

    async def _execute_native_turn(
        self,
        *,
        input: str,
        configured: dict[str, Any],
        context: AgentExecutionContext,
        adapter_name: str,
        run_id: str,
    ) -> CliAgentExecutionResult:
        from guildbotics.intelligences.agent_runtime.diagnostics import (
            record_agent_event,
        )
        from guildbotics.intelligences.agent_runtime.models import (
            AgentEvent,
            AgentEventKind,
            AgentRuntimeError,
            AgentRuntimeErrorCategory,
        )
        from guildbotics.intelligences.agent_runtime.registry import get_native_adapter
        from guildbotics.intelligences.agent_runtime.store import ConversationStore

        store = ConversationStore(context.workspace_data_root)
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
        adapter = await get_native_adapter(self.person_id, adapter_name, run_id)

        async def emit(event: Any) -> None:
            record_agent_event(event, context, conversation)

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
            native_input = _native_turn_input(input, configured, context, conversation)
            await emit(
                AgentEvent(
                    AgentEventKind.TURN,
                    "started",
                    provider_session_id=conversation.provider_session_id,
                    details={"work_kind": context.conversation_key.work_kind},
                )
            )
            terminal = await adapter.run_turn(native_input, context, conversation, emit)
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
            return CliAgentExecutionResult(
                stdout="",
                stderr=str(exc),
                returncode=1,
                error_category=exc.category.value,
                error_details=details,
                provider_session_id=conversation.provider_session_id,
            )
        conversation.provider_session_id = terminal.provider_session_id
        conversation.provider_turn_id = terminal.provider_turn_id
        conversation.provider = adapter_name
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
        compacted = any(
            event.kind is AgentEventKind.TURN and event.name == "context_compaction"
            for event in terminal.events
        )
        conversation.healthy = not compacted
        if compacted:
            conversation.rotation_reason = "context_compaction"
        store.save(conversation)
        return CliAgentExecutionResult(
            stdout=terminal.output.strip(),
            stderr=terminal.stderr.strip(),
            returncode=terminal.returncode,
            provider_session_id=terminal.provider_session_id,
            provider_turn_id=terminal.provider_turn_id,
            finish_reason=terminal.finish_reason,
            usage=dict(terminal.usage),
        )

    def _raise_if_execution_failed(self, result: CliAgentExecutionResult) -> None:
        if result.error_category in {"authentication", "rate_limited"}:
            agent_name = (
                result.error_details.get("cli_agent")
                or self.executable_info.agent_name
                or self.cli_agent
            )
            if result.error_category == "authentication":
                record_correlated_event(
                    event_type="credential.failed",
                    default_source="cli_agent",
                    attributes={
                        "credential.provider": "cli_agent",
                        "credential.cli_agent": agent_name,
                        "error.category": "authentication",
                    },
                    person_id=self.person_id,
                    payload={
                        "provider": "cli_agent",
                        "cli_agent": agent_name,
                        "person_id": self.person_id,
                        "code": "authentication",
                    },
                )
            raise CliAgentExecutionError(
                cli_agent=agent_name,
                result=result,
            )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or "no output"
            raise RuntimeError(
                f"AI CLI tool '{self.cli_agent}' exited with code {result.returncode}: {detail}"
            )
        if not result.stdout:
            detail = result.stderr or "no output"
            raise RuntimeError(
                f"AI CLI tool '{self.cli_agent}' produced no response: {detail}"
            )

    async def _execute_script(
        self,
        input: str,
        cwd: Path | str,
        extra_env: dict[str, str] | None = None,
    ) -> CliAgentExecutionResult:
        """
        Execute the script specified in the coding_agent.run configuration
        in a subprocess with the configured environment variables.

        Args:
            input (str): The input to pass to the script.

        Raises:
            RuntimeError: If the subprocess exits with a non-zero status.
        """
        from guildbotics.intelligences.cli_agents import get_cli_agent_search_path

        env = os.environ.copy()
        env.update(self.executable_info.env)
        _propagate_cwd_workspace_environment(env)
        env["PATH"] = get_cli_agent_search_path(env.get("PATH"))
        gh_config_dir = tempfile.mkdtemp(prefix="guildbotics-gh-config-")
        self._isolate_github_write_credentials(env, gh_config_dir)
        # Per-invocation overlay (e.g. the workflow run id) is applied after
        # credential isolation so callers can scope values to this single
        # subprocess without mutating the shared process environment.
        if extra_env:
            env.update(extra_env)
            from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
            from guildbotics.runtime.person_lease import delegation_environment

            run_id = extra_env.get(TASK_RUN_ENV) or extra_env.get(RUN_ENV) or ""
            if run_id:
                env.update(delegation_environment(run_id))

        # Create temporary file for the prompt input
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            tmp_file.write(input)
            tmp_file.flush()
            temp_file_name = tmp_file.name
        env["PROMPT_FILE"] = temp_file_name

        process: asyncio.subprocess.Process | None = None
        started = time.monotonic()
        try:
            # Launch subprocess in the cloned repository directory
            process = await asyncio.create_subprocess_shell(
                self.executable_info.script,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.logger.info(
                f"AI CLI '{self.cli_agent}' started "
                f"(prompt {len(input.encode('utf-8')) / 1024:.1f}KB)"
            )
            stdout, stderr = await process.communicate()

            stderr_output = stderr.decode(errors="replace")
            response = stdout.decode(errors="replace")
            duration = time.monotonic() - started
            self.logger.info(
                f"AI CLI '{self.cli_agent}' finished rc={process.returncode} "
                f"in {duration:.1f}s "
                f"(response {len(response.encode('utf-8')) / 1024:.1f}KB)"
            )

            if process.returncode != 0:
                self.logger.error(f"AI CLI tool exited with code {process.returncode}")
            error_category, error_details = _parse_cli_agent_error_marker(
                stderr_output, self.logger
            )

            return CliAgentExecutionResult(
                stdout=response.strip(),
                stderr=stderr_output.strip(),
                returncode=process.returncode or 0,
                error_category=error_category,
                error_details=error_details,
            )
        finally:
            # If the await was cancelled (e.g. the service is stopping) before the
            # agent finished, kill the subprocess so a multi-minute agent turn does
            # not keep running detached and block a clean shutdown, and reap it so
            # it does not linger as a zombie.
            if process is not None and process.returncode is None:
                from guildbotics.intelligences.agent_runtime.environment import (
                    terminate_process_tree,
                )

                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(terminate_process_tree(process))
            self.remove_temp_file(temp_file_name)
            shutil.rmtree(gh_config_dir, ignore_errors=True)

    def _isolate_github_write_credentials(
        self, env: dict[str, str], gh_config_dir: str
    ) -> None:
        for key in [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "SSH_AUTH_SOCK",
        ]:
            env.pop(key, None)
        env["GH_CONFIG_DIR"] = gh_config_dir
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_SSH_COMMAND"] = (
            "ssh -F /dev/null -o BatchMode=yes "
            "-o IdentitiesOnly=yes -o IdentityFile=/dev/null"
        )

    def remove_temp_file(self, file_name: str):
        """
        Remove temporary files created during the execution of the AI CLI tool.
        """
        with suppress(OSError):
            os.remove(file_name)

    def _write_request_io(self, prompt: str, kwargs: dict[str, Any]) -> None:
        record_correlated_io(
            io_type="cli_agent.request",
            payload={
                "person_id": self.person_id,
                "brain": self.name,
                "cli_agent": self.cli_agent,
                "cwd": kwargs.get("cwd"),
                "response_class": (
                    self.response_class.__name__ if self.response_class else ""
                ),
                "prompt": prompt,
            },
        )

    def _write_response_io(self, result: CliAgentExecutionResult) -> None:
        stderr = (
            result.stderr
            if transcript_detail() == "full"
            else standard_stderr_tail(result.stderr)
        )
        record_correlated_io(
            io_type="cli_agent.response",
            payload={
                "person_id": self.person_id,
                "brain": self.name,
                "cli_agent": self.cli_agent,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": stderr,
                "stderr_truncated": stderr != result.stderr,
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
