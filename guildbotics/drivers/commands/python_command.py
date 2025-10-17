from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.errors import CustomCommandError
from guildbotics.drivers.commands.models import CommandOutcome
from guildbotics.drivers.commands.utils import stringify_output


class PythonCommand(CommandBase):
    extension = ".py"
    shortcut = "python"

    async def run(self) -> CommandOutcome:
        module = _load_python_module(self.spec.path)
        entry = getattr(module, "main", None)
        if entry is None or not callable(entry):
            raise CustomCommandError(
                f"Python command '{self.spec.path}' must define a callable 'main'."
            )

        sig = inspect.signature(entry)
        params = list(sig.parameters.values())

        args = [
            arg for arg in self.options.args if not (isinstance(arg, str) and "=" in arg)
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
            for key in self.options.params.keys():
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


def _load_python_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise CustomCommandError(
            f"Unable to load python command module from '{path}'."
        )
    module = importlib.util.module_from_spec(spec)
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
