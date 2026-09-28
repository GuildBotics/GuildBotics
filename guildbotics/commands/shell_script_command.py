from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from guildbotics.commands.command_base import CommandBase
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.utils import stringify_output
from guildbotics.utils.child_process import ChildProcess


def _runs_itself(path: Path) -> bool:
    """Return whether the script runs itself, so that its shebang is honored.

    The execute bit alone does not say so: a file mounted from a Windows host
    has every bit set, and a script without a shebang cannot be executed.
    """
    if not os.access(str(path), os.X_OK):
        return False
    with path.open("rb") as script:
        return script.read(2) == b"#!"


class ShellScriptCommand(CommandBase):
    extensions: ClassVar[list[str]] = [".sh"]
    inline_key: ClassVar[str] = "script"

    async def run(self) -> CommandOutcome:
        env = os.environ.copy()
        for key, value in self.options.params.items():
            env[key] = stringify_output(value)

        executable_path = self.spec.path
        temp_file_name: str | None = None
        script = self.spec.get_config_value("script")
        if script is not None:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, encoding="utf-8"
            ) as tmp_file:
                tmp_file.write(script)
                temp_file_name = tmp_file.name
            executable_path = Path(temp_file_name)

        if executable_path is None:
            raise CommandError(
                f"Shell command '{self.spec.name}' is missing a script or executable path."
            )

        try:
            args = (
                [str(executable_path)]
                if _runs_itself(executable_path)
                else ["bash", str(executable_path)]
            )
            args.extend(str(item) for item in self.options.args)
            process = await ChildProcess.start(*args, cwd=str(self.spec.cwd), env=env)
            stdout_data, stderr_data = await process.communicate(
                self.options.message.encode("utf-8")
            )
        except OSError as exc:
            raise CommandError(
                f"Shell command '{self.spec.name}' could not be executed: {exc}"
            ) from exc
        finally:
            if temp_file_name is not None:
                with suppress(OSError):
                    os.remove(temp_file_name)

        if process.returncode != 0:
            error_text = stderr_data.decode("utf-8", errors="replace").strip()
            message = f"Shell command '{self.spec.name}' failed with exit code {process.returncode}."
            if error_text:
                message = f"{message} {error_text}"
            raise CommandError(message)

        text_output = stdout_data.decode("utf-8", errors="replace")
        return CommandOutcome(result=text_output, text_output=text_output)
