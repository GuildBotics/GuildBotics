import asyncio
import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging import Logger
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.common import AgentResponse
from guildbotics.observability import span_scope
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import get_person_config_path, load_yaml_file
from guildbotics.utils.log_utils import get_log_output_dir
from guildbotics.utils.prompt_trace import write_prompt_trace
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

    def __init__(self, script: str, env: dict[str, str] | None = None):
        """
        Initialize the executable information.

        Args:
            script (str): The script to execute.
            env (dict): Environment variables to set for the script.
        """
        self.script = script
        self.env = {} if env is None else env


person_cli_agent_mapping: dict[str, dict[str, ExecutableInfo]] = {}


@dataclass(frozen=True)
class CliAgentExecutionResult:
    stdout: str
    stderr: str
    returncode: int
    error_category: str = ""
    error_details: dict[str, str] = field(default_factory=dict)


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
    """Per-invocation environment overlay passed via ``session_state``.

    Callers (e.g. ticket workflow) put a ``cli_agent_env`` mapping into the
    invoke params so values like the workflow run id reach this single agent
    subprocess only, instead of mutating the process-global ``os.environ``
    (which would race across the scheduler's per-member worker threads).
    """
    overlay = (kwargs.get("session_state") or {}).get("cli_agent_env")
    if not isinstance(overlay, dict):
        return {}
    return {str(key): str(value) for key, value in overlay.items()}


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
    cli_agent_mapping = {}
    for name, executable_info_file in mapping.items():
        executable_info_path = get_person_config_path(
            person_id, f"intelligences/cli_agents/{executable_info_file}"
        )
        executable_info = cast(dict, load_yaml_file(executable_info_path))
        cli_agent_mapping[name] = ExecutableInfo(
            script=executable_info.get("script", ""),
            env=executable_info.get("env", {}),
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
            self.logger.debug(
                f"Running AI CLI tool '{self.cli_agent}' with input:\n{input}\n\n"
            )
            self._write_request_trace(input, kwargs)

            response_file = ""
            log_file = ""
            output_dir = get_log_output_dir()
            if output_dir:
                current_time = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                response_file = str(
                    output_dir / f"cli_agent_response_{current_time}.log"
                )
                log_file = str(output_dir / f"cli_agent_output_{current_time}.log")

            result = await self._execute_script(
                input, response_file, log_file, cwd, _extra_env(kwargs)
            )
            output: Any = result.stdout
            self._write_response_trace(result)
            self._raise_if_execution_failed(result)

            self.logger.debug(
                f"AI CLI tool '{self.cli_agent}' produced output:\n{output}\n\n"
            )
            if self.response_class:
                output = to_response_class(output, self.response_class)
            if isinstance(output, AgentResponse):
                log_file_path = Path(log_file)
                if (
                    output.status == AgentResponse.ASKING
                    and log_file
                    and log_file_path.exists()
                ):
                    output.message = f"{output.message}\n\nSee: {log_file_path.name}"

        return output

    async def run_with_execution_details(
        self, message: str, **kwargs
    ) -> CliAgentExecutionResult:
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        with span_scope("cli_agent"):
            self.logger.debug(
                f"Running AI CLI tool '{self.cli_agent}' with input:\n{input}\n\n"
            )
            self._write_request_trace(input, kwargs)

            response_file = ""
            log_file = ""
            output_dir = get_log_output_dir()
            if output_dir:
                current_time = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                response_file = str(
                    output_dir / f"cli_agent_response_{current_time}.log"
                )
                log_file = str(output_dir / f"cli_agent_output_{current_time}.log")

            result = await self._execute_script(
                input, response_file, log_file, cwd, _extra_env(kwargs)
            )
            self._write_response_trace(result)
        return result

    def _raise_if_execution_failed(self, result: CliAgentExecutionResult) -> None:
        if result.error_category == "rate_limited":
            raise CliAgentExecutionError(
                cli_agent=self.cli_agent,
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
        response_file: str,
        log_file: str,
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

        # Create temporary file for the prompt input
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            tmp_file.write(input)
            tmp_file.flush()
            temp_file_name = tmp_file.name
        env["PROMPT_FILE"] = temp_file_name

        process: asyncio.subprocess.Process | None = None
        try:
            # Launch subprocess in the cloned repository directory
            process = await asyncio.create_subprocess_shell(
                self.executable_info.script,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.logger.info(
                f"Running AI CLI tool '{self.cli_agent}' with script: {self.executable_info.script}"
            )
            self.logger.debug(f"Environment: {self._mask_env(env)}")
            stdout, stderr = await process.communicate()
            self.logger.info(
                f"AI CLI Tool '{self.cli_agent}' finished execution with return code {process.returncode}"
            )

            # Log the outputs
            stderr_output = stderr.decode(errors="replace")
            if stderr_output:
                self.logger.debug(stderr_output)
                if log_file:
                    with open(log_file, "w") as f:
                        f.write(stderr_output)

            response = stdout.decode(errors="replace")
            self.logger.info(f"AI CLI Tool '{self.cli_agent}' response:\n{response}")
            if response_file:
                with open(response_file, "w") as f:
                    f.write(response)

            if process.returncode != 0:
                self.logger.error(f"AI CLI Tool exited with code {process.returncode}")
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
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(asyncio.CancelledError, Exception):
                    # Shield so the reap completes even though our own task is
                    # being cancelled.
                    await asyncio.shield(process.wait())
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

    def _mask_env(self, env: dict[str, str]) -> dict[str, str]:
        sensitive = ("TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY", "ASKPASS")
        return {
            key: "***" if any(part in key.upper() for part in sensitive) else value
            for key, value in env.items()
        }

    def remove_temp_file(self, file_name: str):
        """
        Remove temporary files created during the execution of the AI CLI tool.
        """
        with suppress(OSError):
            os.remove(file_name)

    def _write_request_trace(self, prompt: str, kwargs: dict[str, Any]) -> None:
        write_prompt_trace(
            "cli_agent.request",
            {
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

    def _write_response_trace(self, result: CliAgentExecutionResult) -> None:
        write_prompt_trace(
            "cli_agent.response",
            {
                "person_id": self.person_id,
                "brain": self.name,
                "cli_agent": self.cli_agent,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
