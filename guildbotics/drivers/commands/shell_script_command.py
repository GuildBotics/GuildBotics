from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.errors import CustomCommandError
from guildbotics.drivers.commands.models import CommandOutcome
from guildbotics.drivers.commands.utils import stringify_output


class ShellScriptCommand(CommandBase):
    extension = ".sh"
    shortcut = "script"

    async def run(self) -> CommandOutcome:
        env = os.environ.copy()
        for key, value in self.options.params.items():
            env[key] = stringify_output(value)

        executable_path = self.spec.path
        tmp_file = None
        script = self.spec.get_config_value("script")
        if script is not None:
            tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
            tmp_file.write(script)
            tmp_file.flush()
            tmp_file.close()
            executable_path = Path(tmp_file.name)

        args = (
            [str(executable_path)]
            if os.access(executable_path, os.X_OK)
            else ["bash", str(executable_path)]
        )
        args.extend(str(item) for item in self.options.args)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.spec.cwd,
                env=env,
            )

            stdin_data = self.options.message.encode("utf-8")
            stdout_data, stderr_data = await process.communicate(stdin_data)

            if process.returncode != 0:
                error_text = stderr_data.decode("utf-8", errors="replace").strip()
                message = f"Shell command '{self.spec.name}' failed with exit code {process.returncode}."
                if error_text:
                    message = f"{message} {error_text}"
                raise CustomCommandError(message)

            text_output = stdout_data.decode("utf-8", errors="replace")
            return CommandOutcome(result=text_output, text_output=text_output)

        except FileNotFoundError as exc:  # pragma: no cover - defensive guard
            raise CustomCommandError(
                f"Shell command '{executable_path}' could not be executed."
            ) from exc
        finally:
            if tmp_file is not None:
                try:
                    os.remove(tmp_file.name)
                except OSError:
                    pass
