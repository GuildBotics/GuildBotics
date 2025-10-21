from __future__ import annotations

import os
import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from guildbotics.drivers.commands.models import (
    CommandOutcome,
    CommandSpec,
    InvocationOptions,
)
from guildbotics.runtime.context import Context


class CommandBase(ABC):
    """Base interface for custom command executors."""

    extensions: ClassVar[list[str]]
    inline_key: ClassVar[str]

    def __init__(self, context: Context, spec: CommandSpec, cwd: Path) -> None:
        self._context = context
        self._spec = spec
        self._cwd = cwd
        self._options = self._build_invocation_options(spec)

    @property
    def context(self) -> Context:
        return self._context

    @property
    def spec(self) -> CommandSpec:
        return self._spec

    @property
    def options(self) -> InvocationOptions:
        return self._options

    @property
    def cwd(self) -> Path:
        return self._cwd

    @classmethod
    def get_extensions(cls) -> list[str]:
        return cls.extensions

    @classmethod
    def get_inline_key(cls) -> str:
        return cls.inline_key

    @classmethod
    def is_inline_only(cls) -> bool:
        return len(cls.extensions) == 0

    @classmethod
    def is_inline_command(cls, data: dict[str, Any]) -> bool:
        """Determine if the given data represents an inline command of this type."""
        return cls.get_inline_key() in data

    @abstractmethod
    async def run(self) -> CommandOutcome | None:
        """Execute the command and return its outcome."""

    def _build_invocation_options(self, spec: CommandSpec) -> InvocationOptions:
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
            return self._spec.params.get(text, os.getenv(text, text))

    def parse_inline_args(self, value: str) -> tuple[list[str], dict[str, Any]]:
        """Parse inline arguments from a string and return as args list and params dict."""
        words = shlex.split(value)
        args = []
        params = {}
        for word in words:
            if "=" in word:
                key, val = word.split("=", 1)
                params[key] = val
            else:
                args.append(word)
        return args, params
