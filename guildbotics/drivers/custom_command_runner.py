from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel

from guildbotics.entities.team import Person
from guildbotics.intelligences.functions import (
    get_content,
    preprocess,
    to_dict,
    to_text,
)
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import (
    get_person_config_path,
    load_markdown_with_frontmatter,
)
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.text_utils import (
    get_placeholders_from_args,
    replace_placeholders,
)


class CustomCommandError(RuntimeError):
    """Base error raised when a custom command cannot be executed."""


class PersonSelectionRequiredError(CustomCommandError):
    """Raised when no person could be inferred for a command."""

    def __init__(self, available: Sequence[str]):
        super().__init__("Person selection required.")
        self.available = list(available)


class PersonNotFoundError(CustomCommandError):
    """Raised when the requested person is not part of the team."""

    def __init__(self, identifier: str, available: Sequence[str]):
        super().__init__(f"Person '{identifier}' not found.")
        self.identifier = identifier
        self.available = list(available)


@dataclass
class CommandConfig:
    """Normalized representation of a command or error handler definition."""

    name: str
    path: Path
    params: dict[str, Any] = field(default_factory=dict)
    args: list[Any] | None = None
    stdin_override: str | None = None
    base_dir: Path | None = None
    children: list["CommandConfig"] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    cwd: Path = Path.cwd()
    script: str | None = None
    command_index: int = 0
    config: dict | None = None
    class_resolver: ClassResolver | None = None

    @property
    def kind(self) -> str:
        return self.path.suffix.lower()


@dataclass
class CommandOutcome:
    result: Any
    text_output: str


@dataclass
class InvocationOptions:
    args: list[Any]
    message: str
    params: dict[str, Any]
    output_key: str


class CustomCommandExecutor:
    """Coordinate the execution of main and sub commands."""

    def __init__(
        self,
        context: Context,
        command_name: str,
        command_args: Sequence[str],
        cwd: Path | None = None,
    ) -> None:
        context.set_invoker(self._invoke)
        self._context = context
        self._command_name = command_name
        self._command_args = list(command_args)
        self._registry: dict[str, CommandConfig] = {}
        self._call_stack: list[str] = []
        self._main_directory: Path | None = None
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._main_spec = self._prepare_main_spec()

    async def run(self) -> str:
        await self._execute_with_children(self._main_spec)
        return self._context.pipe

    def _get_placeholders_from_args(
        self, args: Sequence[Any], path: Path
    ) -> dict[str, str]:
        normalized_args = [str(arg) for arg in args]
        return get_placeholders_from_args(normalized_args, path.suffix != ".py")

    def _prepare_main_spec(self) -> CommandConfig:
        path = _resolve_named_command(self._context, self._command_name)
        if not path.exists():
            raise CustomCommandError(
                f"Prompt '{self._command_name}' not found for {self._context.person.person_id}."
            )
        command_params = self._get_placeholders_from_args(self._command_args, path)
        spec = CommandConfig(
            name=self._command_name,
            path=path,
            args=self._command_args,
            params=command_params,
            base_dir=path.parent,
            cwd=self._cwd,
        )
        self._register(spec)
        self._main_directory = path.parent
        return spec

    def _register(self, spec: CommandConfig) -> None:
        spec.base_dir = spec.path.parent
        self._registry[spec.name] = spec

    def _ensure_spec_loaded(
        self, spec: CommandConfig, parent: CommandConfig | None = None
    ) -> None:
        if spec.kind == ".md":
            self._attach_markdown_metadata(spec, parent)

    def _attach_markdown_metadata(
        self, spec: CommandConfig, parent: CommandConfig | None = None
    ) -> None:
        if spec.metadata is not None:
            return
        metadata, _ = self._load_markdown_metadata(spec)
        spec.metadata = metadata
        spec.class_resolver = ClassResolver(
            metadata.get("schema", ""), parent.class_resolver if parent else None
        )
        spec.children = []

        raw_commands = metadata.get("commands")
        if raw_commands is None:
            entries: list[Any] = []
        elif isinstance(raw_commands, Sequence) and not isinstance(
            raw_commands, (str, bytes)
        ):
            entries = list(raw_commands)
        else:
            entries = [str(raw_commands)]

        for entry in entries:
            child = self._build_command_from_entry(entry, spec)
            spec.children.append(child)
            self._register(child)

    def _get_name(self, data: dict, anchor: CommandConfig) -> str:
        name = data.get("name")
        if name:
            return str(name)

        path = data.get("path")
        if path:
            return _default_name_from_path(Path(path))

        return f"{anchor.name}__{anchor.command_index}"

    def _get_path(self, data: dict, anchor: CommandConfig) -> Path:
        if "script" in data:
            return Path(f"<inline-script-{anchor.command_index}>.sh")
        if "prompt" in data:
            return Path(f"<inline-prompt-{anchor.command_index}>.md")

        path_value = data.get("path") or data.get("name")

        if path_value:
            return _resolve_command_reference(
                anchor.path.parent, str(path_value), self._context
            )

        raise CustomCommandError("Command entry requires 'path', 'name' or 'script'.")

    def _parse_command(self, entry: str) -> dict:
        words = shlex.split(entry)
        return {"path": words[0], "args": words[1:]}

    def _build_command_from_entry(
        self, entry: Any, anchor: CommandConfig
    ) -> CommandConfig:
        anchor.command_index = anchor.command_index + 1

        if isinstance(entry, str):
            data = self._parse_command(entry)
        elif isinstance(entry, dict):
            data = entry.copy()
            if "command" in data:
                command_str = str(data.pop("command"))
                command = self._parse_command(command_str)
                data = {**data, **command}
        else:
            raise CustomCommandError("Command entry must be a mapping or string.")

        name = self._get_name(data, anchor)
        resolved_path = self._get_path(data, anchor)

        raw_args = data.get("args", [])
        if raw_args is None:
            args = []
        elif isinstance(raw_args, (list, tuple)):
            args = list(raw_args)
        else:
            args = [raw_args]

        raw_params = data.get("params", {})
        arg_params = self._get_placeholders_from_args(args, resolved_path)
        params = {**anchor.params, **raw_params, **arg_params}

        stdin_override = params.pop("message", None)
        if stdin_override is not None:
            stdin_override = str(stdin_override)

        command_spec = CommandConfig(
            name=str(name),
            path=resolved_path,
            params=params,
            args=args,
            stdin_override=stdin_override,
            base_dir=resolved_path.parent,
            cwd=anchor.cwd,
            script=str(data["script"]) if "script" in data else None,
            command_index=anchor.command_index,
            config=data,
        )

        return command_spec

    def _replace_placeholders(self, text: str) -> Any:
        if not text.startswith("$"):
            return text

        text = text[1:]  # Remove leading $
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1].strip()

        if "." in text:
            parts = text.split(".")
            value: Any = self._context.shared_state
            for part in parts:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    return text  # Placeholder not found, return original text
            return value
        elif text in self._context.shared_state:
            return self._context.shared_state[text]
        else:
            return self._main_spec.params.get(text, os.getenv(text, text))

    async def _execute_with_children(
        self, spec: CommandConfig, parent: CommandConfig | None = None
    ) -> CommandOutcome | None:
        self._ensure_spec_loaded(spec, parent)

        # Execute child commands first
        for child in spec.children:
            await self._execute_with_children(child, spec)

        # Execute the main command
        outcome = await self._execute_spec(spec)
        return outcome

    async def _execute_spec(self, spec: CommandConfig) -> CommandOutcome | None:
        name = spec.name
        if name in self._call_stack:
            cycle = " -> ".join(self._call_stack + [name])
            raise CustomCommandError(f"Cyclic command invocation detected: {cycle}")

        self._call_stack.append(name)

        try:
            options = self._build_invocation_options(spec)
            if spec.kind == ".md":
                outcome = await self._run_markdown_command(spec, options)
            elif spec.kind == ".py":
                outcome = await self._run_python_command(spec, options)
            elif spec.kind == ".sh":
                outcome = await self._run_shell_command(spec, options)
            else:
                raise CustomCommandError(
                    f"Unsupported command type '{spec.path.suffix}' for {spec.name}."
                )
            if outcome is not None:
                self._context.update(
                    options.output_key, outcome.result, outcome.text_output
                )
            return outcome
        finally:
            self._call_stack.pop()

    def _build_invocation_options(self, spec: CommandConfig) -> InvocationOptions:
        if spec.stdin_override is not None:
            message = spec.stdin_override
        else:
            message = self._context.pipe

        params = spec.params.copy()
        for key, value in params.items():
            if isinstance(value, str):
                params[key] = self._replace_placeholders(value)

        args = list(spec.args) if spec.args else []
        for index, arg in enumerate(args):
            if isinstance(arg, str):
                args[index] = str(self._replace_placeholders(arg))

        return InvocationOptions(
            args=args,
            message=str(message),
            params=params,
            output_key=spec.name,
        )

    def _load_markdown_metadata(self, spec: CommandConfig) -> tuple[dict, bool]:
        config = spec.config if isinstance(spec.config, dict) else {}
        prompt = config.get("prompt")
        if spec.metadata is not None:
            return spec.metadata, prompt is not None

        if prompt:
            metadata = config.copy()
            metadata["body"] = str(prompt)
            return metadata, True
        else:
            metadata = load_markdown_with_frontmatter(spec.path)
            return metadata, False

    async def _run_markdown_command(
        self, spec: CommandConfig, options: InvocationOptions
    ) -> CommandOutcome | None:
        metadata, inline = self._load_markdown_metadata(spec)
        if not metadata.get("body"):
            return None

        params = {**self._context.shared_state, **options.params}
        if str(metadata.get("brain", "")).lower() in ["none", "-", "null", "disabled"]:
            template_engine = metadata.get("template_engine", "default")
            d = to_dict(self._context, {})
            params = {**params, **d["session_state"]}
            result = replace_placeholders(metadata["body"], params, template_engine)
            return CommandOutcome(result=result, text_output=result)

        spec.metadata = metadata
        message = options.message if preprocess(self._context, options.message) else ""

        try:
            output = await get_content(
                self._context,
                str(spec.path),
                message,
                params,
                self._cwd,
                metadata if inline else None,
                spec.class_resolver,
            )
        except Exception as exc:  # pragma: no cover - propagate as driver error
            raise CustomCommandError(
                f"Custom command '{spec.name}' execution failed: {exc}"
            ) from exc

        text_output = _stringify_output(output)
        return CommandOutcome(result=output, text_output=text_output)

    def _is_positional(self, params: list[inspect.Parameter], index: int) -> bool:
        if index >= len(params):
            return False
        return params[index].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )

    def _is_keyword(self, params: list[inspect.Parameter], key: str) -> bool:
        for param in params:
            if param.name == key and param.kind in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                return True
        return False

    def _is_var_positional(self, params: list[inspect.Parameter], index: int) -> bool:
        if index >= len(params):
            return False
        return params[index].kind == inspect.Parameter.VAR_POSITIONAL

    async def _run_python_command(
        self, spec: CommandConfig, options: InvocationOptions
    ) -> CommandOutcome:
        module = _load_python_module(spec.path)
        entry = getattr(module, "main", None)
        if entry is None or not callable(entry):
            raise CustomCommandError(
                f"Python command '{spec.path}' must define a callable 'main'."
            )

        sig = inspect.signature(entry)
        params = list(sig.parameters.values())

        args = [
            arg for arg in options.args if not (isinstance(arg, str) and "=" in arg)
        ]
        kwargs = options.params.copy()
        call_args: list[Any] = []
        call_kwargs = {}

        index = 0
        if self._is_positional(params, 0) and params[0].name in ["context", "ctx", "c"]:
            call_args.append(self._context)
            index += 1

        for i, arg in enumerate(args):
            if self._is_positional(params, index):
                call_args.append(arg)
                index += 1
            else:
                if self._is_var_positional(params, index):
                    call_args.extend(args[i:])
                    index += 1
                break

        params = params[index:]
        if len(params) > 0:
            index = 0
            for key in options.params.keys():
                if self._is_keyword(params, key):
                    call_kwargs[key] = kwargs.pop(key)
                    index += 1

            for index in range(index, len(params)):
                if params[index].kind == inspect.Parameter.VAR_KEYWORD:
                    call_kwargs.update(kwargs)

        func_result = entry(*call_args, **call_kwargs)

        if inspect.iscoroutine(func_result):
            func_result = await func_result

        text_output = _stringify_output(func_result)
        return CommandOutcome(result=func_result, text_output=text_output)

    async def _run_shell_command(
        self, spec: CommandConfig, options: InvocationOptions
    ) -> CommandOutcome:
        env = os.environ.copy()
        for key, value in options.params.items():
            env[str(key)] = _stringify_output(value)

        executable_path = spec.path
        tmp_file = None
        if spec.script is not None:
            # Create temporary script file
            tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
            tmp_file.write(spec.script)
            tmp_file.flush()
            tmp_file.close()
            executable_path = Path(tmp_file.name)

        args = (
            [str(executable_path)]
            if os.access(executable_path, os.X_OK)
            else ["bash", str(executable_path)]
        )
        args.extend(str(item) for item in options.args)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=env,
            )

            stdin_data = options.message.encode("utf-8")
            stdout_data, stderr_data = await process.communicate(stdin_data)

            if process.returncode != 0:
                error_text = stderr_data.decode("utf-8", errors="replace").strip()
                message = f"Shell command '{spec.name}' failed with exit code {process.returncode}."
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

    async def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        anchor = self._current_spec()
        spec = self._create_dynamic_spec(anchor, name, *args, **kwargs)
        outcome = await self._execute_with_children(spec)
        return outcome.result if outcome else None

    def _create_dynamic_spec(
        self, anchor: CommandConfig, name: str, *args: Any, **kwargs: Any
    ) -> CommandConfig:
        data = {
            "name": name,
            "args": list(args),
            "params": kwargs,
        }
        spec = self._build_command_from_entry(data, anchor)
        self._register(spec)
        return spec

    def _current_spec(self) -> CommandConfig:
        if self._call_stack:
            current_name = self._call_stack[-1]
            current_spec = self._registry.get(current_name)
            if current_spec is not None:
                return current_spec
        return self._main_spec


async def run_custom_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    person_identifier: str | None = None,
    cwd: Path | None = None,
) -> str:
    """Execute a custom prompt command and return the rendered output."""
    person = _resolve_person(base_context.team.members, person_identifier)
    context = base_context.clone_for(person)
    executor = CustomCommandExecutor(context, command_name, command_args, cwd)
    return await executor.run()


def _resolve_person(members: Sequence[Person], identifier: str | None) -> Person:
    if identifier is None:
        active_members = [member for member in members if member.is_active]
        if len(active_members) == 1:
            return active_members[0]
        available = _list_person_labels(members)
        raise PersonSelectionRequiredError(available)

    person = _find_person(members, identifier)
    if person is None:
        available = _list_person_labels(members)
        raise PersonNotFoundError(identifier, available)
    return person


def _find_person(members: Sequence[Person], identifier: str) -> Person | None:
    lower_identifier = identifier.casefold()
    for member in members:
        if member.person_id.casefold() == lower_identifier:
            return member
    for member in members:
        if member.name.casefold() == lower_identifier:
            return member
    return None


def _list_person_labels(members: Sequence[Person]) -> list[str]:
    labels: list[str] = []
    for member in members:
        label = member.person_id
        if member.name and member.name.casefold() != member.person_id.casefold():
            label = f"{label} ({member.name})"
        labels.append(label)
    return sorted(labels)


def _stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return to_text(output)
    if isinstance(output, dict):
        return to_text(output)
    if isinstance(output, list):
        if output and isinstance(output[0], (BaseModel, dict)):
            return to_text(output)
        return "\n".join(str(item) for item in output)
    return str(output)


def _default_name_from_path(path: Path) -> str:
    if path.name.startswith(".") and path.stem:
        return path.stem
    return path.stem or path.name


def _resolve_named_command(context: Context, identifier: str) -> Path:
    language_code = context.team.project.get_language_code()
    person_id = context.person.person_id
    candidates: list[Path] = []
    identifier_path = Path(identifier)
    if identifier_path.suffix:
        suffixes = [identifier]
    else:
        suffixes = [f"{identifier}.md", f"{identifier}.py", f"{identifier}.sh"]

    for suffix_identifier in suffixes:
        prompts_path = get_person_config_path(
            person_id, f"prompts/{suffix_identifier}", language_code
        )
        if prompts_path.exists():
            return prompts_path

        intelligence_path = get_person_config_path(
            person_id, f"intelligences/{suffix_identifier}", language_code
        )
        if intelligence_path.exists():
            return intelligence_path

    raise CustomCommandError(f"Unable to locate command '{identifier}'.")


def _resolve_command_reference(base_dir: Path, value: str, context: Context) -> Path:
    candidate_path = Path(value)
    if candidate_path.is_absolute():
        if not candidate_path.exists():
            raise CustomCommandError(f"Command file '{candidate_path}' not found.")
        return candidate_path

    anchored = (base_dir / candidate_path).resolve()
    if candidate_path.suffix:
        if anchored.exists():
            return anchored
    else:
        for extension in (".md", ".py", ".sh"):
            extended = anchored.with_suffix(extension)
            if extended.exists():
                return extended

    if anchored.exists():
        return anchored

    resolved = _resolve_named_command(context, value)
    if resolved.exists():
        return resolved
    raise CustomCommandError(f"Command '{value}' could not be resolved.")


def _load_python_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise CustomCommandError(f"Unable to load python command module from '{path}'.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
