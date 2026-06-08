import asyncio
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.common import AgentResponse
from guildbotics.utils.fileio import get_person_config_path, load_yaml_file
from guildbotics.utils.log_utils import get_log_output_dir
from guildbotics.utils.prompt_trace import write_prompt_trace
from guildbotics.utils.text_utils import replace_placeholders


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
            str: A Markdown-formatted prompt ready to send to the CLI agent.
        """
        # Create JSON schema for the response model
        description = replace_placeholders(
            self.description, session_state, template_engine
        )

        return to_plain_text(description, user_input, self.response_class)


class CliAgentBrain(Brain):
    """
    Intelligence that runs a CLI agent.
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
        Run the CLI agent with the provided arguments.

        Args:
            message (str): The message to pass to the agent.
            **kwargs: Arguments to pass to the agent.
        """
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        self.logger.debug(
            f"Running CLI agent '{self.cli_agent}' with input:\n{input}\n\n"
        )
        self._write_request_trace(input, kwargs)

        response_file = ""
        log_file = ""
        output_dir = get_log_output_dir()
        if output_dir:
            current_time = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            response_file = str(output_dir / f"cli_agent_response_{current_time}.log")
            log_file = str(output_dir / f"cli_agent_output_{current_time}.log")

        result = await self._execute_script(input, response_file, log_file, cwd)
        output: Any = result.stdout
        self._write_response_trace(result)
        self._raise_if_execution_failed(result)

        self.logger.debug(
            f"CLI agent '{self.cli_agent}' produced output:\n{output}\n\n"
        )
        if self.response_class:
            output = to_response_class(output, self.response_class)
        if isinstance(output, AgentResponse):
            log_file_path = Path(log_file)
            if output.status == AgentResponse.ASKING and log_file_path.exists():
                output.message = f"{output.message}\n\nSee: {log_file_path.name}"

        return output

    async def run_with_execution_details(
        self, message: str, **kwargs
    ) -> CliAgentExecutionResult:
        cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        self.logger.debug(
            f"Running CLI agent '{self.cli_agent}' with input:\n{input}\n\n"
        )
        self._write_request_trace(input, kwargs)

        response_file = ""
        log_file = ""
        output_dir = get_log_output_dir()
        if output_dir:
            current_time = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            response_file = str(output_dir / f"cli_agent_response_{current_time}.log")
            log_file = str(output_dir / f"cli_agent_output_{current_time}.log")

        result = await self._execute_script(input, response_file, log_file, cwd)
        self._write_response_trace(result)
        return result

    def _raise_if_execution_failed(self, result: CliAgentExecutionResult) -> None:
        if result.returncode != 0:
            detail = result.stderr or result.stdout or "no output"
            raise RuntimeError(
                f"CLI agent '{self.cli_agent}' exited with code {result.returncode}: {detail}"
            )
        if not result.stdout:
            detail = result.stderr or "no output"
            raise RuntimeError(
                f"CLI agent '{self.cli_agent}' produced no response: {detail}"
            )

    async def _execute_script(
        self, input: str, response_file: str, log_file: str, cwd: Path | str
    ) -> CliAgentExecutionResult:
        """
        Execute the script specified in the coding_agent.run configuration
        in a subprocess with the configured environment variables.

        Args:
            input (str): The input to pass to the script.

        Raises:
            RuntimeError: If the subprocess exits with a non-zero status.
        """
        from guildbotics.app_api.cli_agents import get_cli_agent_search_path

        env = os.environ.copy()
        env.update(self.executable_info.env)
        env["PATH"] = get_cli_agent_search_path(env.get("PATH"))
        gh_config_dir = tempfile.mkdtemp(prefix="guildbotics-gh-config-")
        self._isolate_github_write_credentials(env, gh_config_dir)

        # Create temporary file for the prompt input
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            tmp_file.write(input)
            tmp_file.flush()
            temp_file_name = tmp_file.name
        env["PROMPT_FILE"] = temp_file_name

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
                f"Running CLI agent '{self.cli_agent}' with script: {self.executable_info.script}"
            )
            self.logger.debug(f"Environment: {self._mask_env(env)}")
            stdout, stderr = await process.communicate()
            self.logger.info(
                f"CLI Agent '{self.cli_agent}' finished execution with return code {process.returncode}"
            )

            # Log the outputs
            stderr_output = stderr.decode(errors="replace")
            if stderr_output:
                self.logger.debug(stderr_output)
                if log_file:
                    with open(log_file, "w") as f:
                        f.write(stderr_output)

            response = stdout.decode(errors="replace")
            self.logger.info(f"CLI Agent '{self.cli_agent}' response:\n{response}")
            if response_file:
                with open(response_file, "w") as f:
                    f.write(response)

            if process.returncode != 0:
                self.logger.error(f"CLI Agent exited with code {process.returncode}")

            return CliAgentExecutionResult(
                stdout=response.strip(),
                stderr=stderr_output.strip(),
                returncode=process.returncode or 0,
            )
        finally:
            # Clean up temporary prompt file
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
        Remove temporary files created during the execution of the CLI agent.
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
