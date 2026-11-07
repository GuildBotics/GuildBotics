"""The command execution machinery's entry inside a command's isolated
environment: ``/opt/guildbotics/venv/bin/python -m guildbotics.guest.entry``.

The host starts it once per command, in the microVM it booted for the
command, with the command's facts in the environment
(:class:`~guildbotics.guest.host_client.CommandFacts`)
and what to run on standard input (a ``CommandRequest``). It runs the main
command the host resolved -- by its path, never by its name again -- and its
subcommands with a :class:`~guildbotics.runtime.context.Context` made of
what the environment holds: the workspace's configuration, and the command's
window to the host for everything else -- the member's services, the
external inference calls, the run record. It writes how the command ended to
standard output (a ``CommandReply``) and logs to standard error, which the
host logs as its own; it records nothing itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic_core import to_jsonable_python

from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import CommandError
from guildbotics.commands.runner import CommandRunner
from guildbotics.guest.host_client import (
    ClientRunLedger,
    HostClient,
    command_window,
)
from guildbotics.guest.inference import GuestInference
from guildbotics.guest.resolved import ResolvedBrainFactory, team
from guildbotics.guest.window import WindowIntegrationFactory
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
)
from guildbotics.intelligences.agent_runtime.wire import (
    RESOLVED_GUEST_ENV,
    CommandFacts,
    CommandFailure,
    CommandReply,
    CommandRequest,
    ResolvedGuest,
    admits,
)
from guildbotics.intelligences.brains.inference import install_inference
from guildbotics.intelligences.common import find_cli_agent_execution_error
from guildbotics.runtime.context import Context
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from guildbotics.utils.i18n_tool import t


async def run(
    request: CommandRequest,
    facts: CommandFacts,
    client: HostClient,
    *,
    child: bool = False,
) -> CommandReply:
    """Run ``request`` as the command ``facts`` describe, reaching the host
    through ``client``, and say how it ended."""
    try:
        resolved = ResolvedGuest.model_validate_json(
            Path(os.environ[RESOLVED_GUEST_ENV]).read_bytes()
        )
        if resolved.member.get("person_id") != facts.person_id:
            raise CommandError("The resolved guest member does not match this command.")
        os.environ["AGNO_TELEMETRY"] = "false"
        install_inference(GuestInference(resolved, client))
        context = Context(
            WindowIntegrationFactory(client),
            ResolvedBrainFactory(resolved),
            message=request.pipe,
            team=team(resolved),
            resolved_commands=resolved.commands,
            clone_paths=resolved.clone_paths,
        )
        person = next(
            (m for m in context.team.members if m.person_id == facts.person_id),
            None,
        )
        if person is None:
            raise CommandError(f"Person '{facts.person_id}' not found.")
        context = context.clone_for(person)
        if child:
            if not admits(facts.mounts, request.cwd):
                raise CommandError(
                    t(
                        "intelligences.agent_environment.runtime.outside_mounts",
                        path=request.cwd,
                    )
                )
            request.path = str(resolve_named_command(context, request.name))
        if request.invocation is not None:
            context.shared_state[WORKFLOW_INVOCATION_KEY] = request.invocation
        runner = CommandRunner(
            context,
            request.name,
            request.args,
            Path(request.cwd),
            path=Path(request.path),
            mounts=facts.mounts,
            ledger=ClientRunLedger(client, facts),
        )
        try:
            outcome = await runner.run()
        finally:
            await context.aclose()
    except Exception as exc:
        logging.getLogger("guildbotics").exception("The command failed.")
        return CommandReply(failure=_failure(exc))
    return CommandReply(
        result=(
            to_jsonable_python(outcome.result, fallback=str)
            if request.wants_result
            else None
        ),
        text_output=outcome.text_output,
    )


def _failure(exc: Exception) -> CommandFailure:
    """``exc`` as the host rebuilds it: whether it was a command's own
    failure, and the AI CLI tool's failure it came from, whole, so the host
    still tells a rate limit or a refused login from any other failure."""
    found = find_cli_agent_execution_error(exc)
    from guildbotics.intelligences.brains.inference import InferenceFailure

    cause: BaseException | None = exc
    visited: set[int] = set()
    inference = None
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, InferenceFailure):
            inference = cause
            break
        cause = cause.__cause__
    failure = CommandFailure(
        command=isinstance(exc, CommandError),
        type=type(exc).__name__,
        message=str(exc),
    )
    if inference is not None:
        failure.inference_error_type = inference.error_type
        failure.inference_category = inference.category
    if isinstance(found, CliAgentExecutionError):
        failure.cli_agent = found.cli_agent
        failure.cli_agent_message = str(found)
        failure.cli_agent_result = asdict(found.result)
    return failure


async def _run_with_relays(
    request: CommandRequest, facts: CommandFacts, client: HostClient
) -> CommandReply:
    from guildbotics.guest.gateway_relays import relays

    async with relays():
        return await run(request, facts, client)


def main() -> None:
    """Run the command the host asks for, as the host started this process.

    The reply has standard output to itself, on a descriptor no process the
    command starts inherits: what the command or such a process prints goes
    to the log instead, and none of them holds the reply open.
    """
    if len(sys.argv) > 1:
        client = command_window()
        if client is None:
            raise SystemExit(t("runtime.command_entry.environment_required"))
        request = CommandRequest(
            path="", name=sys.argv[1], args=sys.argv[2:], cwd=Path.cwd().as_posix()
        )
        reply = asyncio.run(
            run(request, CommandFacts.read(os.environ), client, child=True)
        )
        if reply.failure:
            print(reply.failure.message, file=sys.stderr)
            raise SystemExit(1)
        print(reply.text_output)
        return
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("guildbotics")
    logger.addHandler(handler)
    level = getattr(
        logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO
    )
    logger.setLevel(level)
    handler.setLevel(level)
    from agno.utils import log as agno_log

    agno_log.logger.setLevel(level)
    for agno_handler in agno_log.logger.handlers:
        agno_handler.setLevel(level)
    request = CommandRequest.model_validate_json(sys.stdin.buffer.read())
    client = command_window()
    assert client is not None
    reply = asyncio.run(
        _run_with_relays(request, CommandFacts.read(os.environ), client)
    )
    with replies:
        replies.write(reply.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
