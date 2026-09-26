from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, ClassVar

from guildbotics.commands.command_base import CommandBase
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.utils import stringify_output


class PythonCommand(CommandBase):
    extensions: ClassVar[list[str]] = [".py"]
    inline_key: ClassVar[str] = "python"

    async def run(self) -> CommandOutcome:
        if self.spec.path is None:
            raise CommandError(
                f"Python command '{self.spec.name}' is missing a file path."
            )
        module = _load_python_module(self.spec.path)
        entry = getattr(module, "main", None)
        if entry is None or not callable(entry):
            raise CommandError(
                f"Python command '{self.spec.path}' must define a callable 'main'."
            )

        sig = inspect.signature(entry)
        params = list(sig.parameters.values())

        args = [
            arg
            for arg in self.options.args
            if not (isinstance(arg, str) and "=" in arg)
        ]
        kwargs = self.options.params.copy()
        call_args: list[Any] = []
        call_kwargs: dict[str, Any] = {}

        index = 0
        if _is_positional(params, 0) and params[0].name in ["context", "ctx", "c"]:
            call_args.append(self.context)
            index += 1

        for i, arg in enumerate(args):
            if _is_positional(params, index):
                call_args.append(arg)
                index += 1
            else:
                if _is_var_positional(params, index):
                    call_args.extend(args[i:])
                    index += 1
                break

        params = params[index:]
        if params:
            assigned_keywords = 0
            for key in self.options.params:
                if _is_keyword(params, key):
                    call_kwargs[key] = kwargs.pop(key)
                    assigned_keywords += 1

            for idx in range(assigned_keywords, len(params)):
                if params[idx].kind == inspect.Parameter.VAR_KEYWORD:
                    call_kwargs.update(kwargs)

        func_result = entry(*call_args, **call_kwargs)
        if inspect.iscoroutine(func_result):
            func_result = await func_result

        text_output = stringify_output(func_result)
        return CommandOutcome(result=func_result, text_output=text_output)


class _NoBytecodeLoader(importlib.machinery.SourceFileLoader):
    """Source loader that never writes a bytecode cache beside the source.

    Command files live in the synchronized ``config/commands/`` tree, where a
    ``__pycache__/*.pyc`` is a binary file the shared-file validation refuses.
    Running a command would therefore leave the user an unsendable change they
    never wrote. Discarding the cache keeps the generated artifact from
    existing at all; the cost is recompiling the command source on each run.
    """

    def set_data(self, path: str, data: Any, *, _mode: int = 0o666) -> None:
        """Discard the bytecode instead of writing it next to the source."""


def _load_python_module(path: Path) -> Any:
    """Execute a command file as a module registered under a path-derived name.

    The module stays in ``sys.modules`` because ``dataclasses`` resolves string
    annotations (``from __future__ import annotations``) through it. The name
    comes from the resolved path instead of the file stem: a command named
    ``inspect.py`` must not replace the standard library module for the rest of
    the process, and commands sharing a stem in different directories must not
    replace each other.
    """
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    name = f"guildbotics_command_{digest}"
    spec = importlib.util.spec_from_file_location(
        name, path, loader=_NoBytecodeLoader(name, str(path))
    )
    if spec is None or spec.loader is None:
        raise CommandError(f"Unable to load python command module from '{path}'.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def _is_positional(params: list[inspect.Parameter], index: int) -> bool:
    if index >= len(params):
        return False
    return params[index].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def _is_keyword(params: list[inspect.Parameter], key: str) -> bool:
    for param in params:
        if param.name == key and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _is_var_positional(params: list[inspect.Parameter], index: int) -> bool:
    if index >= len(params):
        return False
    return params[index].kind == inspect.Parameter.VAR_POSITIONAL
