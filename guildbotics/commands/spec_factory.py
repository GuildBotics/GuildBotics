from __future__ import annotations

import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from guildbotics.commands.arguments import (
    parse_command_argument_definitions,
    resolve_command_argument_params,
)
from guildbotics.commands.discovery import resolve_command_reference
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import (
    command_entries,
    command_output_name,
    normalize_command_entry,
)
from guildbotics.commands.models import CommandSpec
from guildbotics.commands.registry import find_command_class, get_command_types
from guildbotics.intelligences.agent_runtime.host_client import admits
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.text_utils import get_placeholders_from_args

if TYPE_CHECKING:
    from guildbotics.commands.command_base import CommandBase
    from guildbotics.runtime.context import Context


class CommandSpecFactory:
    """Build `CommandSpec` instances from declarative command entries."""

    def __init__(self, context: Context, mounts: Mapping[str, bool]) -> None:
        """
        Args:
            context: The run's context.
            mounts: Where the environment lets a command work.
        """
        self._context = context
        self._mounts = mounts

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
        config = normalize_command_entry(entry)
        anchor.command_index += 1

        name = command_output_name(config, anchor.name, anchor.command_index)
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

        cwd = self._resolve_cwd(config.get("cwd"), anchor.cwd)

        spec = CommandSpec(
            name=name,
            base_dir=path.parent if path else anchor.base_dir,
            command_class=command_class,
            path=path,
            params=params,
            args=args,
            stdin_override=stdin_override,
            cwd=cwd,
            config=config,
            class_resolver=anchor.class_resolver,
        )
        return spec

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

    def _resolve_cwd(self, raw_cwd: Any, default: Path) -> Path:
        """``raw_cwd`` against the calling command's working directory:
        a relative one is relative to it, never to the process's.

        Raises:
            CommandError: If it is outside what the environment lets a
                command work in: nothing of the host is there.
        """
        if raw_cwd is None:
            return default
        cwd = Path(posixpath.normpath((default / Path(str(raw_cwd))).as_posix()))
        if not admits(self._mounts, cwd.as_posix()):
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.outside_mounts",
                    path=cwd,
                )
            )
        return cwd

    def populate_spec(
        self,
        spec: CommandSpec,
        config: dict,
        class_resolver: ClassResolver | None,
    ) -> None:
        if spec.path is None:
            return

        definitions = parse_command_argument_definitions(config)
        spec.params = resolve_command_argument_params(spec.params, definitions)
        spec.class_resolver = ClassResolver(config.get("schema", ""), class_resolver)
        spec.children = []

        for entry in command_entries(config):
            child = self.build_from_entry(spec, entry)
            spec.children.append(child)
