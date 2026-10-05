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
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import ValidationError, validate_call

from guildbotics.commands.agent_turn import RunLedger
from guildbotics.intelligences.agent_environment.provider_state import (
    record_authentication_outcome,
)
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
    TURN_WORKING_DIRECTORY,
    CommandFacts,
    CredentialEntry,
    Entry,
    EventEntry,
    HostCallError,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberBrokerEndpoint,
    member_invocation,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore
from guildbotics.intelligences.brains.inference import AgnoCall, JevCall
from guildbotics.intelligences.brains.span_summary import record_summary
from guildbotics.intelligences.cli_agents import cli_agent_info
from guildbotics.observability import bind_span, correlation_fields
from guildbotics.observability.diagnostics_events import (
    record_correlated_event,
    record_correlated_io,
)
from guildbotics.observability.session_transcripts import recorded_stderr
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    get_workspace_config_dir,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger

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
        "member",
        "agno",
        "jev",
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

    def variables(
        self, endpoint: MemberBrokerEndpoint, mounts: Mapping[str, bool]
    ) -> dict[str, str]:
        """What the command's microVM is started with to reach this window and
        know the command, as it spells the workspace: its ``mounts`` among it,
        and whether work may happen under each."""
        access = current_command_access()
        facts = CommandFacts(
            person_id=self._person_id,
            run_id=self._run_id,
            work_kind=self._work_kind,
            trace_id=str(correlation_fields().get("trace_id") or ""),
            access=access,
            inspected=inspected_directories(access.inspects, self._workspace_root),
            mounts=dict(mounts),
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
        endpoint = turn.broker.endpoint
        return {
            "turn_grant": turn.broker.turn_grant,
            "env": turn.spec.env,
            "cwd": turn.spec.cwd,
            "home": turn.spec.home,
            "mounts": turn.spec.directories,
            "member": {
                "name": endpoint.name,
                "url": endpoint.guest_url,
                "authorization": endpoint.authorization,
            },
        }

    @validate_call
    async def end_turn(self, turn_grant: str) -> dict[str, str]:
        """End the running turn, revoking what it was lent; ``refusal`` is
        why the login it was lent could not be used, if it could not."""
        if self._turn is None or not secrets.compare_digest(
            turn_grant, self._turn[1].broker.turn_grant
        ):
            raise HostCallError("refused", "No such turn of this command is running.")
        context = await self._end()
        return {"refusal": context.login.refusal()}

    async def close(self) -> None:
        """End the grant with its command. A turn the command left running
        -- it was stopped mid-turn -- is ended, and its conversation marked
        unhealthy: the session it was cut short in is not resumed. What
        could not be marked is logged: the command ends with its own outcome.
        """
        if self._turn is None:
            return
        context = await self._end()
        try:
            record = await asyncio.to_thread(
                self._conversations.load, context.conversation_key
            )
            if record is not None:
                await asyncio.to_thread(
                    self._conversations.mark_unhealthy, record, "cancelled"
                )
        except Exception:
            get_logger().exception("Could not mark the cut-short conversation.")

    async def _end(self) -> AgentExecutionContext:
        """End the running turn, and say which it was."""
        assert self._turn is not None
        (context, turn), self._turn = self._turn, None
        self._turning = False
        try:
            await turn.close()
        finally:
            if context.lease is not None:
                context.lease.unbind_run_id(context.run_id)
        return context

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
    ) -> dict[str, Any]:
        self._check_conversation(key)
        record = await asyncio.to_thread(
            partial(
                self._conversations.resolve,
                key,
                policy,
                model=model,
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
        span ended; what it proved of a tool's login the command runs. The
        event types and attributes are the host's own.
        """
        for entry in entries:
            if isinstance(entry, EventEntry):
                self._check_conversation(entry.conversation)
            elif isinstance(entry, (CredentialEntry, SummaryEntry)):
                self._check_tool(entry.tool)
        await asyncio.to_thread(self._write, entries)

    @validate_call
    async def member(self, arguments: list[str], stdin: str = "") -> dict[str, Any]:
        """Run a member command as the grant's member, for the grant's run: what
        the command asks of the member's services itself, as a turn of it
        would; ``stdin`` is what ``--content-stdin`` reads."""
        result = await running_command().member(
            self._person_id,
            arguments,
            member_invocation(
                self._work_kind,
                self._run_id,
                str(correlation_fields().get("trace_id") or ""),
                command_lease(),
            ),
            stdin,
        )
        return result.model_dump()

    @validate_call
    async def agno(self, person_id: str, call: AgnoCall) -> dict[str, Any]:
        """Have the model of the member's slot answer a brain's request."""
        if person_id != self._person_id:
            raise HostCallError("refused", "The model is another member's.")
        # Imported here: only a command that asks loads what the calls need.
        from guildbotics.intelligences.brains.inference_host import DirectInference

        try:
            answer = await DirectInference().agno(person_id, call)
            return answer.model_dump(mode="json", fallback=str)
        except Exception as exc:
            raise _inference_failed(exc) from exc

    @validate_call
    async def jev(self, call: JevCall) -> dict[str, Any]:
        """Ask Jev a brain's questions."""
        from guildbotics.intelligences.brains.inference_host import DirectInference

        try:
            return await DirectInference().jev(call)
        except Exception as exc:
            raise _inference_failed(exc) from exc

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
                elif isinstance(entry, CredentialEntry):
                    self._write_credential(entry)
                else:
                    record_summary(
                        get_logger(),
                        "cli_agent",
                        entry.slot,
                        entry.status,
                        duration_ms=entry.duration_ms,
                        attributes={
                            "agent.kind": "cli_agent",
                            "agent.slot": entry.slot,
                            "agent.adapter": entry.tool,
                        },
                        model=entry.model,
                        model_specified=entry.model_specified,
                        effort=entry.effort,
                        usage=entry.usage,
                    )

    def _write_credential(self, entry: CredentialEntry) -> None:
        """Record what a turn proved about its tool's login here.

        All members share the device/tool outcome used by the status card and
        alerts. Diagnostics retain the member and tool for attribution.
        """
        record_authentication_outcome(cli_agent_info(entry.tool), failed=entry.failed)
        code = "authentication" if entry.failed else ""
        record_correlated_event(
            event_type="credential.failed" if entry.failed else "credential.verified",
            default_source="cli_agent",
            attributes={
                "credential.provider": "cli_agent",
                "credential.cli_agent": entry.tool,
                **({"error.category": code} if code else {}),
            },
            person_id=self._person_id,
            payload={
                "provider": "cli_agent",
                "cli_agent": entry.tool,
                "person_id": self._person_id,
                **({"code": code} if code else {}),
            },
        )

    def _write_event(self, entry: EventEntry) -> None:
        context = AgentExecutionContext(
            person_id=self._person_id,
            run_id=self._run_id,
            cwd=self._workspace_root,
            conversation_key=entry.conversation,
            context_cursor=entry.context_cursor,
            lease=command_lease(),
        )
        event = entry.event
        if TURN_WORKING_DIRECTORY in event.details:
            event = replace(event, details=self._confinement(event.details))
        record_agent_event(
            event,
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

    def _confinement(self, details: Mapping[str, Any]) -> dict[str, Any]:
        """An event's details, with where its turn works replaced by what the
        environment confines it to there, whatever provider runs it.

        Raises:
            HostCallError: ``refused`` for a working directory that is not a
                normalized path.
        """
        kept = dict(details)
        try:
            cwd = host_path(str(kept.pop(TURN_WORKING_DIRECTORY, "")))
        except AgentEnvironmentSpecError as exc:
            raise HostCallError("refused", str(exc)) from exc
        kept["requested_policy"] = running_command().contract.requested_policy(
            cwd, workspace_root=self._workspace_root
        )
        return kept

    def _check_tool(self, tool: str) -> None:
        """Refuse a tool the command does not run."""
        if tool not in running_command().tools:
            raise HostCallError("refused", "The command does not run that tool.")

    def _check_conversation(self, key: ConversationKey) -> None:
        """Refuse a conversation not of the member's work of the grant's run
        with a tool the command runs."""
        if key.person_id != self._person_id:
            raise HostCallError("refused", "The conversation is another member's.")
        self._check_tool(key.adapter)
        self._check_run(self._run_id, key.work_kind)


def _inference_failed(exc: Exception) -> HostCallError:
    """How a failed inference call reaches the command's environment: by its
    kind and reported status, never its message, which may carry credentials
    (the host's log is kept and shown, so it is not written there either).
    The reported status may be an SDK default without an HTTP response.
    """
    kind = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    get_logger().warning("An inference call of a command failed (%s).", kind)
    message = (
        t(
            "intelligences.inference.failed_with_status",
            error_type=kind,
            status=status,
        )
        if status
        else t("intelligences.inference.failed", error_type=kind)
    )
    return HostCallError(
        "failed",
        message,
        {"error_type": kind, **({"status_code": status} if status else {})},
    )
