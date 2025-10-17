from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Sequence

from guildbotics.drivers.commands.discovery import resolve_command_reference
from guildbotics.drivers.commands.errors import CustomCommandError
from guildbotics.drivers.commands.models import CommandSpec
from guildbotics.drivers.commands.registry import get_command_types
from guildbotics.runtime.context import Context
from guildbotics.utils.text_utils import get_placeholders_from_args


class CommandSpecFactory:
    """Build `CommandConfig` instances from declarative command entries."""

    def __init__(self, context: Context) -> None:
        self._context = context

    def build_from_entry(self, anchor: CommandSpec, entry: Any) -> CommandSpec:
        data = self._normalize_entry(entry)
        anchor.command_index += 1

        name = self._resolve_name(data, anchor)
        path = self._resolve_path(data, anchor)
        args = self._normalize_args(data.get("args"))
        params = self._merge_params(anchor, args, data.get("params"), path)

        stdin_override = params.pop("message", None)
        if stdin_override is not None:
            stdin_override = str(stdin_override)

        spec = CommandSpec(
            name=name,
            path=path,
            params=params,
            args=args,
            stdin_override=stdin_override,
            base_dir=path.parent,
            cwd=anchor.cwd,
            command_index=anchor.command_index,
            config=data,
        )
        return spec

    def _normalize_entry(self, entry: Any) -> dict[str, Any]:
        if isinstance(entry, str):
            return self._parse_command(entry)
        if isinstance(entry, dict):
            normalized = dict(entry)
            if "command" in normalized:
                command = self._parse_command(str(normalized.pop("command")))
                normalized = {**command, **normalized}
            return normalized
        raise CustomCommandError("Command entry must be a mapping or string.")

    def _parse_command(self, entry: str) -> dict[str, Any]:
        words = shlex.split(entry)
        if not words:
            raise CustomCommandError("Command entry string cannot be empty.")
        return {"path": words[0], "args": words[1:]}

    def _resolve_name(self, data: dict[str, Any], anchor: CommandSpec) -> str:
        name = data.get("name")
        if name:
            return str(name)

        path_value = data.get("path")
        if path_value:
            return _default_name_from_path(Path(path_value))

        return f"{anchor.name}__{anchor.command_index}"

    def _resolve_path(self, data: dict[str, Any], anchor: CommandSpec) -> Path:
        inline = self._try_inline_path(anchor, data)
        if inline is not None:
            return inline

        path_value = data.get("path") or data.get("name")
        if not path_value:
            raise CustomCommandError(
                "Command entry requires 'path', 'name' or 'script'."
            )

        resolved = resolve_command_reference(
            anchor.path.parent, str(path_value), self._context
        )
        return resolved

    def _try_inline_path(
        self, anchor: CommandSpec, data: dict[str, Any]
    ) -> Path | None:
        for command_cls in get_command_types():
            inline_spec = command_cls.resolve_inline_spec(anchor, data)
            if inline_spec is not None:
                return inline_spec
        return None

    def _normalize_args(self, raw_args: Any) -> list[Any]:
        if raw_args is None:
            return []
        if isinstance(raw_args, (list, tuple)):
            return list(raw_args)
        return [raw_args]

    def _merge_params(
        self,
        anchor: CommandSpec,
        args: Sequence[Any],
        raw_params: Any,
        path: Path,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        params.update(anchor.params)

        if raw_params:
            if isinstance(raw_params, dict):
                params.update(raw_params)
            else:
                raise CustomCommandError(
                    "Command params must be provided as a mapping."
                )

        arg_params = self._get_placeholders_from_args(args, path)
        params.update(arg_params)

        return params

    def _get_placeholders_from_args(
        self, args: Sequence[Any], path: Path
    ) -> dict[str, str]:
        normalized_args = [str(arg) for arg in args]
        return get_placeholders_from_args(normalized_args, path.suffix != ".py")


def _default_name_from_path(path: Path) -> str:
    if path.name.startswith(".") and path.stem:
        return path.stem
    return path.stem or path.name
