import asyncio
import os
import tempfile
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Type, cast

from pydantic import BaseModel

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.common import AgentResponse
from guildbotics.utils.fileio import get_person_config_path, load_yaml_file
from guildbotics.utils.log_utils import get_log_output_dir
from guildbotics.utils.text_utils import replace_placeholders


class ExecutableInfo:
    """
    Information about an executable script.
    """

    def __init__(self, script: str, env: dict = {}, cwd: str | None = None):
        """
        Initialize the executable information.

        Args:
            script (str): The script to execute.
            env (dict): Environment variables to set for the script.
            cwd (str | None): The working directory for the script.
        """
        self.script = script
        self.env = env
        self.cwd = cwd


person_cli_agent_mapping: dict[str, dict[str, ExecutableInfo]] = {}


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
        response_class: Type[BaseModel] | None,
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
        response_class: Type[BaseModel] | None = None,
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
        self.executable_info.cwd = kwargs["cwd"]
        input = self.prompt_info.to_prompt(
            message, kwargs.get("session_state", {}), self.template_engine
        )
        self.logger.debug(
            f"Running CLI agent '{self.cli_agent}' with input:\n{input}\n\n"
        )

        response_file = ""
        log_file = ""
        output_dir = get_log_output_dir()
        if output_dir:
            current_time = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            response_file = str(output_dir / f"cli_agent_response_{current_time}.log")
            log_file = str(output_dir / f"cli_agent_output_{current_time}.log")

        output = await self._execute_script(input, response_file, log_file)

        self.logger.debug(
            f"CLI agent '{self.cli_agent}' produced output:\n{output}\n\n"
        )
        if self.response_class:
            output = to_response_class(output, self.response_class)
        if isinstance(output, AgentResponse):
            output = cast(AgentResponse, output)
            log_file_path = Path(log_file)
            if output.status == AgentResponse.ASKING and log_file_path.exists():
                output.message = f"{output.message}\n\nSee: {log_file_path.name}"

        return output

    async def _execute_script(self, input: str, response_file: str, log_file: str):
        """
        Execute the script specified in the coding_agent.run configuration
        in a subprocess with the configured environment variables.

        Args:
            input (str): The input to pass to the script.

        Raises:
            RuntimeError: If the subprocess exits with a non-zero status.
        """
        # Merge provided env with current environment
        env = (self.executable_info.env or {}).copy()
        env["PATH"] = os.environ.get("PATH", "")

        # Create temporary file for the prompt input
        tmp_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        tmp_file.write(input)
        tmp_file.flush()
        tmp_file.close()
        env["PROMPT_FILE"] = tmp_file.name

        try:
            # Launch subprocess in the cloned repository directory
            process = await asyncio.create_subprocess_shell(
                self.executable_info.script,
                cwd=self.executable_info.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.logger.info(
                f"Running CLI agent '{self.cli_agent}' with script: {self.executable_info.script}"
            )
            self.logger.debug(f"Environment: {env}")
            stdout, stderr = await process.communicate()
            self.logger.info(
                f"CLI Agent '{self.cli_agent}' finished execution with return code {process.returncode}"
            )

            # Log the outputs
            if stderr:
                stderr_output = stderr.decode()
                self.logger.debug(stderr_output)
                if log_file:
                    with open(log_file, "w") as f:
                        f.write(stderr_output)

            response = stdout.decode()
            self.logger.info(f"CLI Agent '{self.cli_agent}' response:\n{response}")
            if response_file:
                with open(response_file, "w") as f:
                    f.write(response)

            if process.returncode != 0:
                self.logger.error(f"CLI Agent exited with code {process.returncode}")

            return response.strip()
        finally:
            # Clean up temporary prompt file
            self.remove_temp_file(tmp_file.name)

    def remove_temp_file(self, file_name: str):
        """
        Remove temporary files created during the execution of the CLI agent.
        """
        try:
            os.remove(file_name)
        except OSError:
            pass
