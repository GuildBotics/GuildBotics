from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from guildbotics.commands.command_base import CommandBase
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.utils import stringify_output

_WINDOWS = os.name == "nt"


def _runs_itself(path: Path) -> bool:
    """Return whether the operating system can execute the script directly.

    Windows has no execute bit -- ``os.access(..., os.X_OK)`` answers yes for
    every file that exists -- and ``CreateProcess`` refuses a ``.sh`` whatever
    its shebang says, so the interpreter runs it there, as it does for a file
    without the bit elsewhere.
    """
    return not _WINDOWS and os.access(str(path), os.X_OK)


def _shell_executable() -> str:
    """Return the shell that runs a script the operating system will not.

    Git for Windows is the POSIX shell a Windows machine running GuildBotics
    already has, and it is not always on PATH: ``git.exe`` is exposed through
    ``cmd`` while ``bash.exe`` stays in ``bin`` beside it.

    Raises:
        CommandError: When this machine has no ``bash``.
    """
    found = shutil.which("bash")
    if found is None and _WINDOWS:
        git = shutil.which("git")
        if git is not None:
            found = shutil.which("bash", path=str(Path(git).parent.parent / "bin"))
    if found is None:
        raise CommandError(
            "No 'bash' was found to run shell script commands with. "
            "Install a POSIX shell and put 'bash' on PATH."
        )
    return found


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
            # The shell reads bytes, and the output is decoded as UTF-8 further
            # down, so the script is written the same way on every platform.
            # Line endings are pinned for the same reason: a carriage return
            # translated in here reaches the shell as part of the command.
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".sh",
                delete=False,
                encoding="utf-8",
                newline="\n",
            ) as tmp_file:
                tmp_file.write(script)
                tmp_file.flush()
                temp_file_name = tmp_file.name
            executable_path = Path(temp_file_name)

        if executable_path is None:
            raise CommandError(
                f"Shell command '{self.spec.name}' is missing a script or executable path."
            )

        args = (
            [str(executable_path)]
            if _runs_itself(executable_path)
            else [_shell_executable(), str(executable_path)]
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
                raise CommandError(message)

            text_output = stdout_data.decode("utf-8", errors="replace")
            return CommandOutcome(result=text_output, text_output=text_output)

        except FileNotFoundError as exc:  # pragma: no cover - defensive guard
            raise CommandError(
                f"Shell command '{executable_path}' could not be executed."
            ) from exc
        finally:
            if temp_file_name is not None:
                with suppress(OSError):
                    os.remove(temp_file_name)
