"""What a command's isolated environment may ask of the host: its grant.

The host entry that runs a command makes one :class:`HostWindow` for the run
(``run_in_environment``), and the command's environment answers the calls of
its microVM with it (:meth:`MemberCapabilityBroker.serve`), each in the
command's own context: its trace, its execution lease, the running command.
The grant holds for the whole run, whether or not the run has a workflow run
of its own, and what it covers is settled by the host, never by the caller:
the member it runs as, the run it records to, and where the command works.

Everything a call carries was written inside the microVM, where the agent's
code runs beside the command's, so it is read as the agent's: the run it
names must be the grant's, the conversation the member's, and a path is
taken back to the host only as far as what the microVM mounted.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import nullcontext
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import ValidationError, validate_call

from guildbotics.commands.agent_turn import RunLedger
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpecError,
    guest_path,
    host_path,
)
from guildbotics.intelligences.agent_runtime.diagnostics import record_agent_event
from guildbotics.intelligences.agent_runtime.environment import (
    TurnEnvironment,
    command_lease,
    current_command_access,
    inspected_directories,
    running_command,
    start_turn_environment,
)
from guildbotics.intelligences.agent_runtime.host_client import (
    COMMAND_ENV,
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    CommandFacts,
    Entry,
    EventEntry,
    HostCallError,
    IoEntry,
)
from guildbotics.intelligences.agent_runtime.member_broker import MemberBrokerEndpoint
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore
from guildbotics.observability import bind_span, correlation_fields
from guildbotics.observability.diagnostics_events import (
    record_correlated_io,
    record_span_summary,
)
from guildbotics.observability.session_transcripts import recorded_stderr
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    get_workspace_config_dir,
)

#: The kinds of work only a workflow run of that kind does.
_WORKFLOW_KINDS = frozenset({"ticket", "chat"})
#: What the command's environment may ask for.
_CALLS = frozenset(
    {
        "begin_turn",
        "end_turn",
        "require_completion",
        "evidence",
        "record_completed",
        "record_completion_missing",
        "resolve",
        "save",
        "mark_unhealthy",
        "record",
    }
)


class HostWindow:
    """One command run's grant.

    Args:
        person_id: The member the command runs as.
        run_id: The run the command records to: its workflow run's, or one
            of its own.
        work_kind: The kind of work its workflow run does (``ticket`` or
            ``chat``), or empty for a command that is no workflow run.
        workspace_root: The workspace the command runs in.
        ledger: The host's run record.
    """

    def __init__(
        self,
        person_id: str,
        run_id: str,
        work_kind: str,
        *,
        workspace_root: Path,
        ledger: RunLedger,
    ) -> None:
        self._person_id = person_id
        self._run_id = run_id
        self._work_kind = work_kind
        self._workspace_root = workspace_root
        self._ledger = ledger
        self._conversations = ConversationStore(workspace_root)
        #: Whether a turn is starting or running: one at a time.
        self._turning = False
        self._turn: tuple[AgentExecutionContext, TurnEnvironment] | None = None

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        """Answer the call ``name`` with ``arguments``.

        Raises:
            HostCallError: ``refused`` for a call the grant does not cover.
        """
        if name not in _CALLS:
            raise HostCallError("refused", f"There is no host call '{name}'.")
        try:
            return await getattr(self, name)(**arguments)
        except ValidationError as exc:
            raise HostCallError(
                "refused",
                f"The host call '{name}' was given {exc.error_count()}"
                " invalid argument(s).",
            ) from exc

    def variables(self, endpoint: MemberBrokerEndpoint) -> dict[str, str]:
        """What the command's microVM is started with to reach this window and
        know the command, as it spells the workspace."""
        access = current_command_access()
        facts = CommandFacts(
            person_id=self._person_id,
            run_id=self._run_id,
            work_kind=self._work_kind,
            trace_id=str(correlation_fields().get("trace_id") or ""),
            access=access,
            inspected=inspected_directories(access.inspects, self._workspace_root),
        )
        return {
            HOST_URL_ENV: endpoint.guest_host_url,
            HOST_TOKEN_ENV: endpoint.token,
            COMMAND_ENV: facts.dump(),
            GUILDBOTICS_WORKSPACE_ROOT: guest_path(self._workspace_root),
            GUILDBOTICS_CONFIG_DIR: guest_path(
                get_workspace_config_dir(self._workspace_root)
            ),
        }

    @validate_call
    async def begin_turn(
        self,
        tool: str,
        cwd: str,
        run_id: str,
        work_kind: str,
        work_identity: str,
        participant_labels: str = "",
    ) -> dict[str, Any]:
        """Start a turn of ``tool`` in the command's microVM, working in
        ``cwd`` as the microVM spells it, and lend it its login.

        The turn holds what a turn started on the host holds: the command's
        execution lease, bound to its run while it lasts, and the member
        broker, whose MCP server and grant the answer names.
        """
        if self._turning:
            raise HostCallError("refused", "A turn of this command is running.")
        self._check_run(run_id, work_kind)
        try:
            where = host_path(cwd)
        except AgentEnvironmentSpecError as exc:
            raise HostCallError("refused", str(exc)) from exc
        lease = command_lease()
        context = AgentExecutionContext(
            person_id=self._person_id,
            run_id=run_id,
            cwd=where,
            workspace_data_root=self._workspace_root,
            conversation_key=ConversationKey(
                self._person_id, tool, work_kind, work_identity
            ),
            lease=lease,
            participant_labels=participant_labels,
            trace_id=str(correlation_fields().get("trace_id") or ""),
        )
        # Nothing above waits, so no other turn starts in between.
        self._turning = True
        try:
            if lease is not None:
                lease.bind_run_id(run_id)
            turn = await start_turn_environment(context, tool)
        except BaseException:
            if lease is not None:
                lease.unbind_run_id(run_id)
            self._turning = False
            raise
        self._turn = (context, turn)
        return {
            "turn_grant": turn.broker.turn_grant,
            "env": turn.spec.env,
            "cwd": turn.spec.cwd,
            "member_server": turn.broker.mcp_server,
        }

    @validate_call
    async def end_turn(self, turn_grant: str) -> dict[str, str]:
        """End the running turn, revoking what it was lent; ``refusal`` is
        why the login it was lent could not be used, if it could not."""
        if self._turn is None or not secrets.compare_digest(
            turn_grant, self._turn[1].broker.turn_grant
        ):
            raise HostCallError("refused", "No such turn of this command is running.")
        (context, turn), self._turn = self._turn, None
        self._turning = False
        try:
            await turn.close()
        finally:
            if context.lease is not None:
                context.lease.unbind_run_id(context.run_id)
        return {"refusal": context.login.refusal()}

    @validate_call
    async def require_completion(self, run_id: str) -> None:
        self._check_run(run_id)
        await asyncio.to_thread(self._ledger.require_completion, run_id)

    @validate_call
    async def evidence(self, run_id: str) -> list[dict[str, Any]]:
        self._check_run(run_id)
        return await asyncio.to_thread(self._ledger.evidence, run_id)

    @validate_call
    async def record_completed(self, run_id: str, attempt: int) -> None:
        self._check_run(run_id)
        await asyncio.to_thread(self._ledger.record_completed, run_id, attempt)

    @validate_call
    async def record_completion_missing(
        self, run_id: str, attempt: int, max_attempts: int, error: str
    ) -> None:
        self._check_run(run_id)
        await asyncio.to_thread(
            self._ledger.record_completion_missing,
            run_id,
            attempt,
            max_attempts,
            error,
        )

    @validate_call
    async def resolve(
        self,
        key: ConversationKey,
        policy: ResumePolicy,
        model: str = "",
        settings_fingerprint: str = "",
    ) -> dict[str, Any]:
        self._check_conversation(key)
        record = await asyncio.to_thread(
            partial(
                self._conversations.resolve,
                key,
                policy,
                model=model,
                settings_fingerprint=settings_fingerprint,
            )
        )
        return asdict(record)

    @validate_call
    async def save(self, record: ConversationRecord) -> dict[str, Any]:
        self._check_conversation(record.key)
        await asyncio.to_thread(self._conversations.save, record)
        return asdict(record)

    @validate_call
    async def mark_unhealthy(
        self, record: ConversationRecord, reason: str
    ) -> dict[str, Any]:
        self._check_conversation(record.key)
        await asyncio.to_thread(self._conversations.mark_unhealthy, record, reason)
        return asdict(record)

    @validate_call
    async def record(self, entries: list[Entry]) -> None:
        """Write ``entries``, in order, in the command's trace, each under the
        span the microVM opened for it.

        Only what a turn records can be written: its events, as the member's
        conversations of the grant's run; its request and response; how its
        span ended. The event types and attributes are the host's own.
        """
        for entry in entries:
            if isinstance(entry, EventEntry):
                self._check_conversation(entry.conversation)
        await asyncio.to_thread(self._write, entries)

    def _write(self, entries: list[Entry]) -> None:
        for entry in entries:
            with bind_span(entry.span) if entry.span else nullcontext():
                if isinstance(entry, EventEntry):
                    self._write_event(entry)
                elif isinstance(entry, IoEntry):
                    payload = entry.payload
                    if entry.io_type == "cli_agent.response":
                        stderr = str(payload.get("stderr") or "")
                        kept = recorded_stderr(stderr)
                        payload = {
                            **payload,
                            "stderr": kept,
                            "stderr_truncated": kept != stderr,
                        }
                    record_correlated_io(io_type=entry.io_type, payload=payload)
                else:
                    record_span_summary(
                        status=entry.status,
                        model=entry.model,
                        effort=entry.effort,
                        duration_ms=entry.duration_ms,
                        usage=entry.usage,
                        attributes={
                            "agent.kind": "cli_agent",
                            "agent.slot": entry.slot,
                        },
                    )

    def _write_event(self, entry: EventEntry) -> None:
        context = AgentExecutionContext(
            person_id=self._person_id,
            run_id=self._run_id,
            cwd=self._workspace_root,
            workspace_data_root=self._workspace_root,
            conversation_key=entry.conversation,
            context_cursor=entry.context_cursor,
            lease=command_lease(),
        )
        record_agent_event(
            entry.event,
            context,
            ConversationRecord(key=entry.conversation, generation=entry.generation),
        )

    def _check_run(self, run_id: str, work_kind: str | None = None) -> None:
        """Refuse a run other than the grant's, or work of a kind only
        another run does."""
        if run_id != self._run_id:
            raise HostCallError("refused", "The run is not this command's.")
        if work_kind is not None and (
            work_kind != self._work_kind
            if self._work_kind
            else work_kind in _WORKFLOW_KINDS
        ):
            raise HostCallError("refused", "The work is not this command's.")

    def _check_conversation(self, key: ConversationKey) -> None:
        """Refuse a conversation not of the member's work of the grant's run
        with a tool the command runs."""
        if key.person_id != self._person_id:
            raise HostCallError("refused", "The conversation is another member's.")
        if key.adapter not in running_command().tools:
            raise HostCallError("refused", "The command does not run that tool.")
        self._check_run(self._run_id, key.work_kind)
