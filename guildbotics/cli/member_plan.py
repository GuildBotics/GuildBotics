"""Describe client-side inputs from the backend's Click definitions."""

from __future__ import annotations

import copy
import io
from pathlib import Path
from typing import Any

import click

from guildbotics.cli.member import MemberCall, _HostPath, member


def plan(arguments: list[str]) -> dict[str, Any]:
    """Parse without invoking callbacks or touching any caller path."""
    output = io.StringIO()
    command: click.Command = member
    parent = None
    remaining = arguments[:]
    names: list[str] = []
    try:
        while True:
            cloned = copy.copy(command)
            cloned.params = [copy.copy(p) for p in command.params]
            for param in cloned.params:
                if isinstance(param.type, _HostPath):
                    param.type = click.STRING
            ctx = cloned.make_context(
                "guildbotics member" if parent is None else names[-1],
                remaining[:],
                parent=parent,
                obj=MemberCall(cwd=Path("/"), stdin="", stdout=output),
            )
            if not isinstance(command, click.Group):
                break
            tail = [*ctx._protected_args, *ctx.args]
            if not tail:
                output.write(ctx.get_help() + "\n")
                return {
                    "result": {
                        "exit_code": 0,
                        "stdout": output.getvalue(),
                        "stderr": "",
                    }
                }
            name, *remaining = tail
            sub = command.get_command(ctx, name)
            if sub is None:
                raise click.UsageError(f"No such command {name!r}.", ctx)
            names.append(name)
            parent, command = ctx, sub
        rewritten = arguments[:]
        if (
            ctx.params.get("content_stdin")
            and ctx.params.get("content_file") is not None
        ):
            from guildbotics.utils.i18n_tool import t

            raise click.UsageError(t("cli.member.content.exclusive"), ctx)
        file = ctx.params.get("content_file")
        file_source = None
        if file is not None:
            # Walk options by their arity, so a title equal to an option is
            # still a title. The client never interprets the command grammar.
            options = {
                opt: p
                for p in command.params
                if isinstance(p, click.Option)
                for opt in p.opts
            }
            offset = len(arguments) - len(remaining)
            index = offset
            rewritten = arguments[:offset]
            while index < len(arguments):
                token = arguments[index]
                spelling, equals, _ = token.partition("=")
                option = options.get(spelling)
                count = 1 + (
                    option.nargs if option and not option.is_flag and not equals else 0
                )
                if option is not None and option.name == "content_file":
                    file_source = {
                        "index": index if equals else index + 1,
                        "offset": len(spelling) + 1 if equals else 0,
                    }
                    rewritten.append("--content-stdin")
                else:
                    rewritten.extend(arguments[index : index + count])
                index += count
        return {
            "output_format": ctx.params.get("output_format", "json"),
            "download": names
            in (["ticket", "attachment", "download"], ["pr", "artifact", "download"]),
            "arguments": rewritten,
            "content_file": file,
            "file_source": file_source,
            "stdin": bool(ctx.params.get("content_stdin")) or file is not None,
            "memory": names[:1] == ["memory"],
            "git": (
                {"operation": names[-1], **ctx.params}
                if names[:1] == ["git"]
                and ctx.params.get("workspace_mode") == "current"
                else None
            ),
        }
    except click.exceptions.Exit as exc:
        return {
            "result": {
                "exit_code": exc.exit_code,
                "stdout": output.getvalue(),
                "stderr": "",
            }
        }
    except click.ClickException as exc:
        stderr = io.StringIO()
        exc.show(stderr)
        return {
            "result": {
                "exit_code": exc.exit_code,
                "stdout": "",
                "stderr": stderr.getvalue(),
            }
        }
