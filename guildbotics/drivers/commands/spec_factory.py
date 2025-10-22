from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from guildbotics.drivers.commands.discovery import resolve_command_reference
from guildbotics.drivers.commands.errors import CommandError
from guildbotics.drivers.commands.models import CommandSpec
from guildbotics.drivers.commands.registry import find_command_class, get_command_types
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.text_utils import get_placeholders_from_args

if TYPE_CHECKING:
    from guildbotics.drivers.commands.command_base import CommandBase
    from guildbotics.runtime.context import Context


class CommandSpecFactory:
    """Build `CommandSpec` instances from declarative command entries."""

    def __init__(self, context: Context) -> None:
        self._context = context

    def prepare_main_spec(
        self,
        path: Path,
        command_name: str,
        command_args: list[str],
        cwd: Path,
    ) -> CommandSpec:
        kind = path.suffix.lower()
        command_params = self._get_placeholders_from_args(command_args, kind)
        spec = CommandSpec(
            name=command_name,
            base_dir=path.parent,
            command_class=find_command_class(kind),
            path=path,
            args=command_args,
            params=command_params,
            cwd=cwd,
        )
        return spec

    def build_from_entry(self, anchor: CommandSpec, entry: Any) -> CommandSpec:
        config = self._normalize_entry(entry)
        anchor.command_index += 1

        name = self._resolve_name(config, anchor)
        path = None
        inline_command = self._is_inline_command(config, anchor)
        if inline_command:
            kind = ""
            command_class = inline_command
        else:
            path, kind = self._resolve_path(config, anchor)
            command_class = find_command_class(kind)
        args = self._normalize_args(config.get("args"))
        params = self._merge_params(anchor, args, config.get("params"), kind)

        stdin_override = params.pop("message", None)
        if stdin_override is not None:
            stdin_override = str(stdin_override)

        spec = CommandSpec(
            name=name,
            base_dir=path.parent if path else anchor.base_dir,
            command_class=command_class,
            path=path,
            params=params,
            args=args,
            stdin_override=stdin_override,
            cwd=anchor.cwd,
            command_index=anchor.command_index,
            config=config,
            class_resolver=anchor.class_resolver,
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
        raise CommandError("Command entry must be a mapping or string.")

    def _parse_command(self, entry: str) -> dict[str, Any]:
        words = shlex.split(entry)
        if not words:
            raise CommandError("Command entry string cannot be empty.")
        return {"path": words[0], "args": words[1:]}

    def _resolve_name(self, data: dict[str, Any], anchor: CommandSpec) -> str:
        name = data.get("name")
        if name:
            return str(name)

        path_value = data.get("path")
        if path_value:
            return self._default_name_from_path(Path(path_value))

        return f"{anchor.name}__{anchor.command_index}"

    def _resolve_path(
        self, data: dict[str, Any], anchor: CommandSpec
    ) -> tuple[Path, str]:
        path_value = data.get("path") or data.get("name")
        if not path_value:
            raise CommandError("Command entry requires 'path', 'name' or 'script'.")

        resolved = resolve_command_reference(
            anchor.base_dir, str(path_value), self._context
        )
        return resolved, resolved.suffix.lower()

    def _is_inline_command(
        self, data: dict[str, Any], anchor: CommandSpec
    ) -> type[CommandBase] | None:
        for command_cls in get_command_types():
            inline_command = command_cls.is_inline_command(data)
            if inline_command:
                return command_cls
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
        args: list[Any],
        raw_params: Any,
        kind: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        params.update(anchor.params)

        if raw_params:
            if isinstance(raw_params, dict):
                params.update(raw_params)
            else:
                raise CommandError("Command params must be provided as a mapping.")

        arg_params = self._get_placeholders_from_args(args, kind)
        params.update(arg_params)

        return params

    def _get_placeholders_from_args(self, args: list[Any], kind: str) -> dict[str, str]:
        normalized_args = [str(arg) for arg in args]
        return get_placeholders_from_args(normalized_args, kind != ".py")

    def _default_name_from_path(self, path: Path) -> str:
        if path.name.startswith(".") and path.stem:
            return path.stem
        return path.stem or path.name

    def populate_spec(
        self,
        spec: CommandSpec,
        config: dict,
        class_resolver: ClassResolver | None,
    ) -> None:
        if spec.path is None:
            return

        spec.class_resolver = ClassResolver(config.get("schema", ""), class_resolver)
        spec.children = []

        raw_commands = config.get("commands")
        if raw_commands is None:
            entries: list[Any] = []
        elif isinstance(raw_commands, Sequence) and not isinstance(
            raw_commands, (str, bytes)
        ):
            entries = list(raw_commands)
        else:
            entries = [str(raw_commands)]

        for entry in entries:
            child = self.build_from_entry(spec, entry)
            spec.children.append(child)
