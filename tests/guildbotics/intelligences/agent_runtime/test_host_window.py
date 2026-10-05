"""The command's window to the host, reached the way its microVM reaches it:
the client and the real window in one process, over the broker's server."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import CommandAccess
from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatPostResult, ChatServiceError
from guildbotics.integrations.code_hosting_service import RepositoryReadError
from guildbotics.integrations.window import (
    MemberCommandError,
    WindowChatService,
    WindowCodeHostingService,
    WindowIntegrationFactory,
)
from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpecError,
    guest_path,
    host_environment,
    host_path,
)
from guildbotics.intelligences.agent_runtime import environment
from guildbotics.intelligences.agent_runtime.host_client import (
    COMMAND_ENV,
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    MEMBER_BROKER_TOKEN_ENV,
    TURN_WORKING_DIRECTORY,
    ClientConversationStore,
    ClientRunLedger,
    CommandFacts,
    CredentialEntry,
    EventEntry,
    HostCallError,
    HostClient,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.agent_runtime.host_window import (
    HostWindow,
    _inference_failed,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore
from guildbotics.intelligences.brains import inference_host
from guildbotics.intelligences.brains.inference import AgnoCall, inference
from guildbotics.intelligences.brains.inference_host import DirectInference
from guildbotics.intelligences.brains.jev import JevBrain
from guildbotics.intelligences.effort import ResolvedEffort
from guildbotics.observability import SpanContext, diagnostics_events, trace_scope
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    get_workspace_root,
)
from guildbotics.utils.i18n_tool import set_language, t
from tests.conftest import COMMAND_ENVIRONMENT_VARIABLES
from tests.guildbotics.intelligences.agent_runtime.contract_doubles import (
    command_at,
    settle_contract,
)
from tests.guildbotics.intelligences.agent_runtime.test_environment import (
    _Booted,
    _device,
)
from tests.guildbotics.intelligences.brains.test_inference import _Model

_RUN = "run-1"


@pytest.fixture(autouse=True)
def _default_contract(monkeypatch) -> None:
    settle_contract(monkeypatch, AccessContract())


@pytest.fixture
def written(monkeypatch) -> list[dict[str, Any]]:
    """What the host writes to diagnostics, in order."""
    records: list[dict[str, Any]] = []

    class Store:
        def record(self, record: dict[str, Any]) -> None:
            records.append(record)

    monkeypatch.setattr(diagnostics_events, "_store", Store)
    return records


@dataclass
class _Ledger:
    """The host's run record, as a test states it."""

    completed: bool = True
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def require_completion(self, run_id: str) -> None:
        self.calls.append(("require_completion", (run_id,)))
        if not self.completed:
            raise RuntimeError(f"Task run '{run_id}' is not completed.")

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        self.calls.append(("evidence", (run_id,)))
        return [{"evidence_type": "pr_comment", "url": "https://example.test/1"}]

    def record_completed(self, run_id: str, attempt: int) -> None:
        self.calls.append(("record_completed", (run_id, attempt)))

    def record_completion_missing(
        self, run_id: str, attempt: int, max_attempts: int, error: str
    ) -> None:
        self.calls.append(
            ("record_completion_missing", (run_id, attempt, max_attempts, error))
        )


@dataclass
class _Command:
    client: HostClient
    ledger: _Ledger
    repository: Path
    trace_id: str
    grant: HostWindow


@asynccontextmanager
async def _command(
    monkeypatch,
    tmp_path,
    *tools: str,
    work_kind: str = "",
    access: CommandAccess = CommandAccess(),
    logins: tuple[str, ...] = (),
) -> AsyncIterator[_Command]:
    """A command of aiko in ``repository`` of the test's workspace, running
    ``tools`` (Claude Code by default), for the run ``run-1``, in a trace of
    the scheduler's, on a device where ``logins`` (the tools by default) are
    logged in: its window, and the client its microVM reaches it with."""
    tools = tools or ("claude",)
    _device(monkeypatch, tmp_path, *(logins or tools))
    ledger = _Ledger()
    repository = get_workspace_root() / "repository"
    repository.mkdir()
    grant = HostWindow(
        "aiko", _RUN, work_kind, workspace_root=get_workspace_root(), ledger=ledger
    )
    with trace_scope(
        "scheduler", person_id="aiko", attributes={"service_run_id": "svc-1"}
    ) as trace:
        async with command_at(repository, tools, access, host=grant):
            endpoint = environment.running_command().endpoint
            yield _Command(
                HostClient(endpoint.host_url, endpoint.token),
                ledger,
                repository,
                trace.trace_id,
                grant,
            )


def _key(work_kind: str = "manual", person_id: str = "aiko") -> ConversationKey:
    return ConversationKey(person_id, "claude", work_kind, "work-1")


async def _begin(command: _Command, tool: str = "claude", **overrides: Any):
    arguments: dict[str, Any] = {
        "cwd": guest_path(command.repository),
        "run_id": _RUN,
        "conversation": ConversationKey("aiko", tool, "manual", "work-1"),
        **overrides,
    }
    return await command.client.begin_turn(tool, arguments.pop("cwd"), **arguments)


@pytest.mark.asyncio
async def test_a_turn_is_lent_its_login_and_what_it_was_refused_comes_back(
    tmp_path, monkeypatch
):
    """The host starts the turn as it would its own -- the login lent, the
    lease bound to the run, the member broker's grant issued -- and ending
    it revokes all of it; the gateway's refusal is the end's answer."""
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.auth_gateway import (
        CredentialGateway,
    )

    upstream: list[httpx.Request] = []
    stand_ins: list[str] = []

    class Refusing(CredentialGateway):
        def __init__(self, *args, **kwargs):
            def refuse(request: httpx.Request) -> httpx.Response:
                upstream.append(request)
                return httpx.Response(401)

            super().__init__(*args, transport=httpx.MockTransport(refuse), **kwargs)

        def lend(self, tokens, stand_in):
            stand_ins.append(stand_in)
            super().lend(tokens, stand_in)

    monkeypatch.setattr(environment, "CredentialGateway", Refusing)
    tool = environment.cli_agent_info("copilot")
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(monkeypatch, tmp_path, "copilot") as command:
            turn = await _begin(command, "copilot")
            broker = environment.running_command()._broker
            assert broker.turn_grant == turn.turn_grant
            assert lease.metadata.run_id == _RUN
            assert turn.cwd == guest_path(command.repository)
            assert turn.env[MEMBER_BROKER_TOKEN_ENV]
            endpoint = broker.endpoint
            assert turn.member == {
                "name": endpoint.name,
                "url": endpoint.guest_url,
                "authorization": endpoint.authorization,
            }
            # What the provider's own sandbox mirrors of the microVM.
            (booted,) = _Booted.booted
            assert turn.home == booted.spec.home
            assert turn.mounts == {
                **{mount.guest: mount.readonly for mount in booted.spec.mounts},
                # The copy of the working directory, on the microVM's own disk.
                guest_path(command.repository): False,
            }
            (base_url,) = {
                turn.env[name] for name in tool.provision.credential_broker.base_url_env
            }
            gateway = base_url.replace("host.microsandbox.internal", "127.0.0.1")
            headers = {"authorization": f"Bearer {stand_ins[-1]}"}
            async with httpx.AsyncClient() as guest:
                await guest.get(f"{gateway.rstrip('/')}/models", headers=headers)

                refusal = await command.client.end_turn(turn.turn_grant)

                after = await guest.get(
                    f"{gateway.rstrip('/')}/models", headers=headers
                )
            with pytest.raises(RuntimeError):
                broker.turn_grant
            assert lease.metadata.run_id == ""
    finally:
        lease.release()

    assert refusal == t(
        "intelligences.agent_environment.tool.login_refused",
        tool=tool.label,
        command=provider_state.login_command("copilot"),
    )
    # Once revoked, the stand-in opens nothing.
    assert len(upstream) == 1
    assert after.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [("tool", "not_started_for"), ("elsewhere", "outside_mounts")],
)
async def test_a_turn_the_microvm_was_not_started_for_is_refused(
    tmp_path, monkeypatch, change, message
):
    """Held to the command's microVM exactly as a turn started on the host
    is; the command's next turn still runs."""
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(
            monkeypatch, tmp_path, logins=("claude", "codex")
        ) as command:
            elsewhere = tmp_path / "elsewhere"
            with pytest.raises(AgentRuntimeError) as refused:
                if change == "tool":
                    await _begin(command, "codex")
                else:
                    await _begin(command, cwd=guest_path(elsewhere))
            # A refused turn leaves the lease to the run's next turn.
            assert lease.metadata.run_id == ""
            turn = await _begin(command)
            await command.client.end_turn(turn.turn_grant)
    finally:
        lease.release()

    assert refused.value.category is AgentRuntimeErrorCategory.CONFIGURATION
    tool = environment.cli_agent_info("codex")
    key = f"intelligences.agent_environment.runtime.{message}"
    assert str(refused.value) == t(key, path=elsewhere, tool=tool.label)
    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cwd", ["relative/path", "/work/../etc", "/work/./here", "/work//here", "/work/"]
)
async def test_a_working_directory_is_read_only_as_the_microvm_writes_one(
    tmp_path, monkeypatch, cwd
):
    async with _command(monkeypatch, tmp_path) as command:
        with pytest.raises(HostCallError) as refused:
            await _begin(command, cwd=cwd)
        # No turn started: the command's next one may.
        await command.client.end_turn((await _begin(command)).turn_grant)

    assert refused.value.category == "refused"


@pytest.mark.asyncio
async def test_the_run_record_is_read_and_reported_to_through_the_window(
    tmp_path, monkeypatch
):
    async with _command(monkeypatch, tmp_path) as command:
        ledger = ClientRunLedger(command.client)
        await asyncio.to_thread(ledger.require_completion, _RUN)
        evidence = await asyncio.to_thread(ledger.evidence, _RUN)
        await asyncio.to_thread(ledger.record_completed, _RUN, 2)
        await asyncio.to_thread(
            ledger.record_completion_missing, _RUN, 1, 3, "not completed"
        )
        command.ledger.completed = False
        with pytest.raises(HostCallError) as missing:
            await asyncio.to_thread(ledger.require_completion, _RUN)

    assert evidence == [
        {"evidence_type": "pr_comment", "url": "https://example.test/1"}
    ]
    assert command.ledger.calls[:4] == [
        ("require_completion", (_RUN,)),
        ("evidence", (_RUN,)),
        ("record_completed", (_RUN, 2)),
        ("record_completion_missing", (_RUN, 1, 3, "not completed")),
    ]
    # What the turn's loop records as the attempt's error.
    assert str(missing.value) == f"Task run '{_RUN}' is not completed."


@pytest.mark.asyncio
async def test_the_conversation_ledger_is_kept_by_the_host(tmp_path, monkeypatch):
    async with _command(monkeypatch, tmp_path) as command:
        store = ClientConversationStore(command.client)
        record = await asyncio.to_thread(
            store.resolve, _key(), ResumePolicy.AUTO, model="opus"
        )
        record.provider_session_id = "session-1"
        record.turn_count = 1
        await asyncio.to_thread(store.save, record)
        saved = ConversationStore(get_workspace_root()).load(_key())
        await asyncio.to_thread(store.mark_unhealthy, record, "cancelled")
        marked = ConversationStore(get_workspace_root()).load(_key())

    assert saved is not None and saved.provider_session_id == "session-1"
    assert saved.model == "opus" and saved.turn_count == 1
    # What the host saved comes back into the caller's record.
    assert record.updated_at and record.created_at == saved.created_at
    assert marked is not None and not marked.healthy
    assert (record.healthy, record.rotation_reason) == (False, "cancelled")


@pytest.mark.asyncio
async def test_records_are_written_in_the_commands_trace_under_the_span_named(
    tmp_path, monkeypatch, written
):
    """In the trace as the command runs it -- its source and attributes
    included -- and each under the span the microVM opened, in the order sent
    at once. Where a turn works becomes what the environment confines it to
    there, and what it proved of its tool's login is the device's too."""
    from guildbotics.intelligences.agent_environment import provider_state

    span = SpanContext("span-1", "call-1", "parent-1", "cli_agent")
    tool = environment.cli_agent_info("claude")
    provider_state.record_authentication_outcome(tool, failed=False)
    async with _command(
        monkeypatch, tmp_path, access=CommandAccess(read_only=True)
    ) as command:
        await command.client.record(
            [
                IoEntry(
                    span=span, io_type="cli_agent.request", payload={"prompt": "p"}
                ),
                EventEntry(
                    span=span,
                    conversation=_key(),
                    generation=2,
                    context_cursor="7",
                    event=AgentEvent(
                        AgentEventKind.TURN,
                        "started",
                        details={
                            "work_kind": "manual",
                            TURN_WORKING_DIRECTORY: guest_path(command.repository),
                        },
                    ),
                ),
                IoEntry(
                    span=span,
                    io_type="cli_agent.response",
                    payload={"stdout": "done", "stderr": "e" * 70_000},
                ),
                CredentialEntry(span=span, tool="claude", failed=True),
                SummaryEntry(
                    span=span,
                    slot="default",
                    tool="claude",
                    model_specified=True,
                    status="finished",
                    model="m",
                    duration_ms=1500,
                ),
            ]
        )

    assert [(record["kind"], record["type"]) for record in written] == [
        ("io", "cli_agent.request"),
        ("event", "agent_runtime.turn"),
        ("io", "cli_agent.response"),
        ("event", "credential.failed"),
        ("event", "span.finished"),
    ]
    for record in written:
        assert record["trace_id"] == command.trace_id
        assert record["source"] == "scheduler"
        assert record["attributes"]["service_run_id"] == "svc-1"
        assert (record["span_id"], record["parent_id"]) == ("span-1", "parent-1")
        assert (record["call_id"], record["span"]) == ("call-1", "cli_agent")
    event = written[1]["attributes"]
    assert event["agent.run_id"] == _RUN
    assert event["agent.conversation_id"] == _key().stable_id
    assert event["agent.conversation_generation"] == 2
    details = written[1]["payload"]["details"]
    assert TURN_WORKING_DIRECTORY not in details
    assert details["requested_policy"]["read_only"] is True
    # Spelled as the device spells its paths.
    assert details["requested_policy"]["filesystem"]["working_directory"] == (
        f"<workspace>{os.sep}repository"
    )
    # The transcript keeps what its detail says of a response's stderr.
    assert written[2]["payload"]["stderr_truncated"] is True
    assert written[3]["payload"] == {
        "provider": "cli_agent",
        "cli_agent": "claude",
        "person_id": "aiko",
        "code": "authentication",
    }
    assert provider_state.authentication_failed(tool)
    assert written[4]["attributes"]["agent.slot"] == "default"
    assert written[4]["attributes"]["agent.adapter"] == "claude"
    assert written[4]["payload"]["model_specified"] is True


@pytest.mark.asyncio
async def test_a_command_without_a_running_grant_answers_nothing(tmp_path):
    broker = MemberCapabilityBroker()
    await broker.start()
    try:
        endpoint = broker.endpoint
        client = HostClient(endpoint.host_url, endpoint.token)
        with pytest.raises(HostCallError) as refused:
            await client.acall("evidence", run_id=_RUN)
        wrong = HostClient(endpoint.host_url, "not-the-token")
        with pytest.raises(HostCallError) as unauthorized:
            await wrong.acall("evidence", run_id=_RUN)
    finally:
        await broker.close()

    assert refused.value.category == "refused"
    assert "No GuildBotics command" in str(refused.value)
    assert unauthorized.value.category == "refused"


@pytest.mark.asyncio
async def test_the_grant_ends_with_the_command(tmp_path, monkeypatch):
    async with _command(monkeypatch, tmp_path) as command:
        pass

    with pytest.raises(HostCallError) as expired:
        await command.client.acall("evidence", run_id=_RUN)

    assert expired.value.category == "unavailable"


_OTHERS = {
    "another run's record": ("evidence", {"run_id": "run-2"}),
    "another member's conversation": (
        "resolve",
        {"key": asdict(_key(person_id="yuki")), "policy": "auto"},
    ),
    "a tool the command does not run": (
        "resolve",
        {
            "key": asdict(ConversationKey("aiko", "codex", "manual", "w")),
            "policy": "auto",
        },
    ),
    "a workflow's work in a command of none": (
        "resolve",
        {"key": asdict(_key("ticket")), "policy": "auto"},
    ),
    "another run's completion": ("record_completed", {"run_id": "run-2", "attempt": 1}),
    "another run's completion check": ("require_completion", {"run_id": "run-2"}),
    "another run's missing completion": (
        "record_completion_missing",
        {"run_id": "run-2", "attempt": 1, "max_attempts": 2, "error": "e"},
    ),
    "saving another member's conversation": (
        "save",
        {"record": {"key": asdict(_key(person_id="yuki"))}},
    ),
    "marking another member's conversation": (
        "mark_unhealthy",
        {"record": {"key": asdict(_key(person_id="yuki"))}, "reason": "r"},
    ),
    "a turn of another run": (
        "begin_turn",
        {
            "tool": "claude",
            "cwd": "/",
            "run_id": "run-2",
            "work_kind": "manual",
            "work_identity": "w",
        },
    ),
    "an event of another member": (
        "record",
        {
            "entries": [
                EventEntry(
                    span=None,
                    conversation=_key(person_id="yuki"),
                    generation=0,
                    event=AgentEvent(AgentEventKind.TURN, "started"),
                ).model_dump(mode="json")
            ]
        },
    ),
    "a record the host does not write": (
        "record",
        {"entries": [{"type": "io", "span": None, "io_type": "span.completed"}]},
    ),
    "a login of a tool the command does not run": (
        "record",
        {
            "entries": [
                CredentialEntry(span=None, tool="codex", failed=False).model_dump(
                    mode="json"
                )
            ]
        },
    ),
    "a summary of a tool the command does not run": (
        "record",
        {
            "entries": [
                SummaryEntry(
                    span=None,
                    slot="default",
                    tool="codex",
                    model_specified=False,
                    status="finished",
                ).model_dump(mode="json")
            ]
        },
    ),
    "a turn starting where no path is": (
        "record",
        {
            "entries": [
                EventEntry(
                    span=None,
                    conversation=_key(),
                    generation=0,
                    event=AgentEvent(
                        AgentEventKind.TURN,
                        "started",
                        details={TURN_WORKING_DIRECTORY: "/work/../etc"},
                    ),
                ).model_dump(mode="json")
            ]
        },
    ),
    "a call there is not": ("read_file", {"path": "/etc/passwd"}),
    "an argument there is not": ("evidence", {"run_id": _RUN, "path": "/"}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_OTHERS))
async def test_the_grant_covers_its_own_member_run_and_calls_only(
    tmp_path, monkeypatch, written, case
):
    name, arguments = _OTHERS[case]
    async with _command(monkeypatch, tmp_path) as command:
        with pytest.raises(HostCallError) as refused:
            await command.client.acall(name, **arguments)
        started = environment.running_command()._broker._context

    assert refused.value.category == "refused"
    assert command.ledger.calls == [] and written == []
    assert started is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grant", "turn", "allowed"),
    [
        ("ticket", "ticket", True),
        ("ticket", "chat", False),
        ("ticket", "manual", False),
        ("", "troubleshooting", True),
        ("", "chat", False),
    ],
)
async def test_a_turn_does_the_work_of_the_grants_run(
    tmp_path, monkeypatch, grant, turn, allowed
):
    """A workflow run's turns do its kind of work; a command that is no
    workflow run does none of a workflow's."""
    async with _command(monkeypatch, tmp_path, work_kind=grant) as command:
        conversation = ConversationKey("aiko", "claude", turn, "work-1")
        if allowed:
            started = await _begin(command, conversation=conversation)
            await command.client.end_turn(started.turn_grant)
        else:
            with pytest.raises(HostCallError):
                await _begin(command, conversation=conversation)


@pytest.mark.asyncio
async def test_a_second_turn_is_refused_while_one_runs(tmp_path, monkeypatch):
    async with _command(monkeypatch, tmp_path) as command:
        first = await _begin(command)
        with pytest.raises(HostCallError):
            # Refused, not left waiting for the running turn to end.
            await asyncio.wait_for(_begin(command), 10)
        with pytest.raises(HostCallError):
            await command.client.end_turn("not-the-grant")
        await command.client.end_turn(first.turn_grant)


@pytest.mark.asyncio
async def test_the_microvm_is_told_the_command_and_its_window(tmp_path, monkeypatch):
    access = CommandAccess(read_only=True, inspects=frozenset({"config"}))
    (get_workspace_root() / ".guildbotics" / "config").mkdir(parents=True)
    async with _command(monkeypatch, tmp_path, access=access) as command:
        shared = environment.running_command()
        endpoint = shared.endpoint
        mounts = shared._mounts
    (booted,) = _Booted.booted
    variables = {
        name: value
        for name, value in booted.spec.env.items()
        if name not in host_environment()
    }

    facts = CommandFacts.read(variables)
    assert facts == CommandFacts(
        person_id="aiko",
        run_id=_RUN,
        work_kind="",
        trace_id=command.trace_id,
        access=access,
        inspected=environment.inspected_directories(
            access.inspects, get_workspace_root()
        ),
        mounts=mounts,
    )
    # Where the command may work: what its contract opened, never what
    # GuildBotics bound for itself.
    assert mounts[guest_path(command.repository)] is True
    assert mounts[environment.CODE_MOUNT.guest] is False
    assert variables[HOST_URL_ENV] == endpoint.guest_host_url
    assert variables[HOST_TOKEN_ENV] == endpoint.token
    assert variables[GUILDBOTICS_WORKSPACE_ROOT] == guest_path(get_workspace_root())
    assert variables[GUILDBOTICS_CONFIG_DIR] == guest_path(
        get_workspace_root() / ".guildbotics" / "config"
    )
    assert set(variables) == {
        HOST_URL_ENV,
        HOST_TOKEN_ENV,
        COMMAND_ENV,
        GUILDBOTICS_WORKSPACE_ROOT,
        GUILDBOTICS_CONFIG_DIR,
    }
    # The suite run inside a command inherits them; each test sets them aside.
    assert (
        set(variables) - {GUILDBOTICS_WORKSPACE_ROOT} <= COMMAND_ENVIRONMENT_VARIABLES
    )


def test_a_guest_path_is_taken_back_to_the_host_path_it_spells(tmp_path) -> None:
    for path in (tmp_path, tmp_path / "a b" / "c"):
        assert host_path(guest_path(path)) == path
    for guest in ("", "relative", "/a/../b", "/a/./b", "/a//b", "/a/"):
        with pytest.raises(AgentEnvironmentSpecError):
            host_path(guest)


@pytest.mark.parametrize(
    "module",
    [
        "guildbotics.intelligences.agent_runtime.host_client",
        "guildbotics.intelligences.brains.agno_agent",
        "guildbotics.intelligences.brains.jev",
        "guildbotics.integrations.window",
    ],
)
def test_what_runs_in_the_environment_loads_nothing_only_the_host_may_hold(
    module: str,
) -> None:
    """The client, the brains, and the member's services run inside the
    command's microVM, beside the command's machinery."""
    probe = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    loaded = json.loads(
        subprocess.run(
            [sys.executable, "-c", probe], check=True, capture_output=True, text=True
        ).stdout
    )
    host_only = {
        "agno",
        "keyring",
        "mcp",
        "microsandbox",
        "starlette",
        "uvicorn",
        "guildbotics.intelligences.agent_runtime.environment",
        "guildbotics.intelligences.agent_runtime.member_broker",
        "guildbotics.intelligences.brains.inference_host",
        "guildbotics.observability.diagnostics_events",
    }
    assert host_only.isdisjoint(module.split(".")[0] for module in loaded)
    assert host_only.isdisjoint(loaded)


@pytest.mark.asyncio
async def test_a_read_only_command_holds_no_lease_for_its_turns(tmp_path, monkeypatch):
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        access = CommandAccess(read_only=True)
        async with _command(monkeypatch, tmp_path, access=access) as command:
            turn = await _begin(command)
            assert lease.metadata.run_id == ""
            await command.client.end_turn(turn.turn_grant)
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_turns_asked_for_at_once_are_refused_but_one(tmp_path, monkeypatch):
    async with _command(monkeypatch, tmp_path) as command:
        results = await asyncio.wait_for(
            asyncio.gather(_begin(command), _begin(command), return_exceptions=True),
            10,
        )
        [started] = [each for each in results if not isinstance(each, BaseException)]
        [refused] = [each for each in results if isinstance(each, BaseException)]
        await command.client.end_turn(started.turn_grant)

    assert isinstance(refused, HostCallError) and refused.category == "refused"


@pytest.mark.asyncio
async def test_a_call_being_answered_ends_before_the_command_closes_what_it_uses(
    tmp_path, monkeypatch
):
    """A turn still starting when the command ends starts before the command
    closes its microVM, and is not left holding it."""
    lending, lend = asyncio.Event(), asyncio.Event()
    async with _command(monkeypatch, tmp_path) as command:
        lent = environment._lend

        async def slow(*args: Any, **kwargs: Any) -> Any:
            lending.set()
            await lend.wait()
            return await lent(*args, **kwargs)

        monkeypatch.setattr(environment, "_lend", slow)
        turn = asyncio.create_task(_begin(command))
        waiting = asyncio.create_task(lending.wait())
        await asyncio.wait({turn, waiting}, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()
        assert lending.is_set(), turn.exception()
        [booted] = _Booted.booted
        asyncio.get_running_loop().call_later(0.2, lend.set)

    assert (await turn).turn_grant
    assert booted.closed
    with pytest.raises(HostCallError):
        await command.client.acall("evidence", run_id=_RUN)


@asynccontextmanager
async def _served(host) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """A broker answering the window's calls with ``host``; a client with its
    token, and the window's URL."""
    import contextvars

    broker = MemberCapabilityBroker()
    broker.serve(host, contextvars.copy_context())
    await broker.start()
    endpoint = broker.endpoint
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": endpoint.authorization}
        ) as client:
            yield client, endpoint.host_url
    finally:
        await broker.close()


async def _echo(name: str, arguments: dict[str, Any]) -> Any:
    return {"name": name, **arguments}


@pytest.mark.asyncio
async def test_the_window_answers_only_the_names_it_is_reached_by() -> None:
    async with _served(_echo) as (client, url):
        port = url.rsplit(":", 1)[1].split("/")[0]
        statuses = {
            host: (
                await client.post(f"{url}/x", json={}, headers={"Host": host})
            ).status_code
            for host in (
                f"host.microsandbox.internal:{port}",
                "localhost",
                f"evil.example:{port}",
                f"127.0.0.1.evil.example:{port}",
            )
        }

    assert statuses == {
        f"host.microsandbox.internal:{port}": 200,
        "localhost": 200,
        f"evil.example:{port}": 421,
        f"127.0.0.1.evil.example:{port}": 421,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_a_call_larger_than_the_limit_is_refused(monkeypatch, chunked) -> None:
    from guildbotics.intelligences.agent_runtime import member_broker

    monkeypatch.setattr(member_broker, "_MAX_REQUEST_BYTES", 1024)
    body = json.dumps({"data": "x" * 4096}).encode()

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), 512):
            yield body[start : start + 512]

    async with _served(_echo) as (client, url):
        response = await client.post(f"{url}/x", content=chunks() if chunked else body)

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "The call is too large."


@pytest.mark.asyncio
async def test_a_call_that_takes_too_long_or_answers_too_much_fails(
    monkeypatch,
) -> None:
    from guildbotics.intelligences.agent_runtime import member_broker

    monkeypatch.setattr(member_broker, "_COMMAND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(member_broker, "_MAX_OUTPUT_BYTES", 64)

    async def host(name: str, arguments: dict[str, Any]) -> Any:
        if name == "slow":
            await asyncio.sleep(1)
        if name == "broken":
            raise ValueError("Host operation failed.")
        return "x" * 128

    async with _served(host) as (client, url):
        slow = HostClient(url, client.headers["Authorization"].split()[1])
        with pytest.raises(HostCallError) as timed_out:
            await slow.acall("slow")
        with pytest.raises(HostCallError) as too_much:
            await slow.acall("large")
        with pytest.raises(HostCallError) as broken:
            await slow.acall("broken")

    assert (timed_out.value.category, str(timed_out.value)) == (
        "failed",
        "The host call timed out.",
    )
    assert (too_much.value.category, str(too_much.value)) == (
        "failed",
        "The call's result is too large.",
    )
    assert (broken.value.category, str(broken.value), broken.value.details) == (
        "failed",
        "Host operation failed.",
        {},
    )


@pytest.fixture
def in_the_environment(monkeypatch):
    """This process as the command's microVM: what it asks of the host goes
    through the window the test names, as its boot variables would say."""

    def enter(endpoint) -> None:
        monkeypatch.setenv(HOST_URL_ENV, endpoint.host_url)
        monkeypatch.setenv(HOST_TOKEN_ENV, endpoint.token)

    return enter


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Every call the microVM sends, as the wire carries it."""
    calls: list[tuple[str, str]] = []
    send = HostClient.acall

    async def acall(self: HostClient, name: str, **arguments: Any) -> Any:
        calls.append((name, json.dumps(arguments)))
        return await send(self, name, **arguments)

    monkeypatch.setattr(HostClient, "acall", acall)
    return calls


_ANSWER_SCHEMA = "class Answer(BaseModel):\n    text: str\n"


@pytest.mark.asyncio
async def test_a_brain_in_the_environment_has_the_host_ask_its_model(
    tmp_path, monkeypatch, written, in_the_environment, sent
):
    """The brain expands its template and reads the answer where it runs; the
    host asks the model of the member's slot with the schema of the output,
    and records the call under the brain's span, in the command's trace."""
    model = _Model(monkeypatch, {"text": "Tea it is."}, person_id="aiko")
    async with _command(monkeypatch, tmp_path) as command:
        in_the_environment(environment.running_command().endpoint)
        assert not isinstance(inference(), DirectInference)
        brain = SimpleBrainFactory().create_brain(
            "aiko",
            "functions/answer",
            "en",
            logging.getLogger("test"),
            {
                "body": "Answer about {topic}.",
                "schema": _ANSWER_SCHEMA,
                "response_class": "Answer",
            },
        )
        answer = await brain.run(
            "hello", session_state={"topic": "tea", "context": object()}
        )

    assert type(answer).__name__ == "Answer"
    assert answer.model_dump() == {"text": "Tea it is."}
    assert brain.execution.model == "test-model"
    (asked,) = model.asked
    assert asked["message"] == "hello"
    assert asked["description"] == "Answer about tea."
    assert asked["session_state"] == {"topic": "tea"}
    schema = asked["output_schema"].model_json_schema()
    assert schema == type(answer).model_json_schema()
    assert [name for name, _ in sent] == ["agno"]
    request, response, summary = written
    assert (request["type"], response["type"]) == ("llm.request", "llm.response")
    assert request["payload"]["description"] == "Answer about tea."
    assert summary["type"] == "span.finished"
    for record in written:
        assert record["trace_id"] == command.trace_id
        assert record["span"] == "llm"
    assert request["span_id"] == summary["span_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "withheld"),
    [
        (
            {"schema": _ANSWER_SCHEMA, "response_class": "Answer"},
            ("class Answer", "BaseModel", "response_class"),
        ),
        (
            {"response_class": "guildbotics.intelligences.common.MessageResponse"},
            ("guildbotics.intelligences", "response_class"),
        ),
    ],
)
async def test_what_a_template_names_to_load_never_reaches_the_host(
    tmp_path, monkeypatch, in_the_environment, sent, config, withheld
):
    """The host is sent the JSON Schema of the output: never the source that
    defines its class, nor the name it is loaded by."""
    _Model(monkeypatch, "{}", person_id="aiko")
    async with _command(monkeypatch, tmp_path):
        in_the_environment(environment.running_command().endpoint)
        brain = SimpleBrainFactory().create_brain(
            "aiko",
            "functions/answer",
            "en",
            logging.getLogger("test"),
            {"body": "Answer.", **config},
        )
        await brain.run("hello")

    ((name, arguments),) = sent
    assert name == "agno"
    assert json.loads(arguments)["call"]["output_schema"]["type"] == "object"
    for text in withheld:
        assert text not in arguments


@pytest.mark.asyncio
async def test_jev_is_asked_by_the_host(tmp_path, monkeypatch, in_the_environment):
    asked: list[Any] = []

    async def request(_root, method, path, payload):
        asked.append(payload)
        return {"model": "jev-1", "answers": {"q": 1}}

    monkeypatch.setattr(inference_host, "request", request)
    async with _command(monkeypatch, tmp_path):
        in_the_environment(environment.running_command().endpoint)
        brain = JevBrain("aiko", "chat_decision", logging.getLogger("test"))
        result = await brain.run(json.dumps({"state": {"s": 1}, "questions": {}}))

    assert result == {"model": "jev-1", "answers": {"q": 1}}
    assert asked == [{"state": {"s": 1}, "questions": {}, "model": "jev-latest"}]


@pytest.mark.asyncio
async def test_the_model_asked_for_is_only_the_grants_members(tmp_path, monkeypatch):
    model = _Model(monkeypatch, "reply", person_id="kenji")
    call = AgnoCall(
        brain="functions/reply",
        slot="default",
        effort=ResolvedEffort(),
        description="",
        message="hello",
    )
    async with _command(monkeypatch, tmp_path) as command:
        with pytest.raises(HostCallError) as refused:
            await command.client.acall(
                "agno", person_id="kenji", call=call.model_dump(mode="json")
            )

    assert refused.value.category == "refused"
    assert model.asked == []


@pytest.mark.asyncio
async def test_a_failed_model_call_reaches_the_environment_by_its_kind_alone(
    tmp_path, monkeypatch, written, in_the_environment, caplog
):
    """Its message may carry the key it was made with; the host records the
    failed span."""
    _Model(monkeypatch, RuntimeError("401 key sk-secret"), person_id="aiko")
    async with _command(monkeypatch, tmp_path):
        in_the_environment(environment.running_command().endpoint)
        brain = SimpleBrainFactory().create_brain(
            "aiko", "functions/reply", "en", logging.getLogger("test"), {"body": "Hi."}
        )
        with pytest.raises(CommandError) as failed:
            await brain.run("hello")

    assert str(failed.value) == t(
        "intelligences.inference.failed", error_type="RuntimeError"
    )
    assert "sk-secret" not in str(failed.value)
    # Nor is it logged: the host's log is kept and shown.
    assert "sk-secret" not in caplog.text
    assert written[-1]["type"] == "span.failed"


@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize("source", ["exception", "http_response", "sdk_default"])
def test_inference_failure_text_is_safe_and_localized(language, source, caplog):
    from agno.exceptions import ModelProviderError

    set_language(language)
    secret = "synthetic-private-key"
    status = None
    if source == "http_response":
        status = 429
        error = httpx.HTTPStatusError(
            secret,
            request=httpx.Request("POST", "https://provider.test"),
            response=httpx.Response(status),
        )
    elif source == "sdk_default":
        error = ModelProviderError(secret)
        # The SDK assigns a status even when there was no HTTP response.
        status = error.status_code
        assert status == 502
    else:
        error = RuntimeError(secret)

    failure = _inference_failed(error)

    assert failure.category == "failed"
    assert failure.details == {
        "error_type": type(error).__name__,
        **({"status_code": status} if status else {}),
    }
    assert str(failure) == t(
        "intelligences.inference.failed_with_status"
        if status
        else "intelligences.inference.failed",
        error_type=type(error).__name__,
        status=status,
    )
    assert "intelligences.inference" not in str(failure)
    if status:
        assert (
            "報告されたステータス" if language == "ja" else "reported status"
        ) in str(failure)
    assert secret not in json.dumps(failure.payload())
    assert secret not in caplog.text


class _Chat:
    """The member's chat as Slack would answer it."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return {"general": "C9"}.get(channel_name)

    async def post_message(self, channel_id: str, text: str, **_: Any):
        self.posted.append((channel_id, text))
        return ChatPostResult(channel_id, "100.1", "")


@pytest.fixture
def chat(monkeypatch) -> _Chat:
    """The chat of the member the member commands resolve."""
    from guildbotics.cli import member as member_cli
    from guildbotics.entities.team import Person, Project, Team

    service = _Chat()
    person = Person(person_id="aiko", name="Aiko")
    context = SimpleNamespace(
        team=Team(project=Project(name="demo"), members=[person]),
        logger=logging.getLogger("test"),
        get_chat_service=lambda: service,
    )
    monkeypatch.setattr(
        member_cli, "resolve_member_context", lambda _person: (context, person)
    )
    return service


@pytest.mark.asyncio
async def test_the_commands_chat_is_the_members_chat_commands(
    tmp_path, monkeypatch, chat
):
    """Through the window, as the grant's member for the grant's run."""
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(monkeypatch, tmp_path) as command:
            service = WindowIntegrationFactory(command.client).create_chat_service(
                logging.getLogger("test"), Person(person_id="aiko", name="Aiko"), None
            )
            general = await service.resolve_channel_id("general")
            missing = await service.resolve_channel_id("nowhere")
            posted = await service.post_message("C9", "Good morning!")
    finally:
        lease.release()

    assert (general, missing) == ("C9", None)
    assert posted == ChatPostResult("C9", "100.1", "")
    assert chat.posted == [("C9", "Good morning!")]
    (evidence,) = TaskRunStore().evidence(_RUN)
    assert evidence["evidence_type"] == "chat_post"


@pytest.mark.asyncio
async def test_a_read_only_command_can_read_its_chat_but_not_post(
    tmp_path, monkeypatch, chat
):
    """Its member commands hold no lease, even while the member holds one."""
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(
            monkeypatch, tmp_path, access=CommandAccess(read_only=True)
        ) as command:
            service = WindowChatService(command.client, "aiko")
            general = await service.resolve_channel_id("general")
            with pytest.raises(ChatServiceError) as refused:
                await service.post_message("C9", "Good morning!")
    finally:
        lease.release()

    assert general == "C9"
    assert str(refused.value).endswith(t("cli.member.lease.invalid_delegation"))
    assert chat.posted == []


@pytest.mark.asyncio
async def test_read_only_repository_read_uses_the_member_grant(
    tmp_path, monkeypatch, chat
):
    from guildbotics.cli import member as member_cli
    from guildbotics.editions.simple.simple_integration_factory import (
        SimpleIntegrationFactory,
    )
    from guildbotics.integrations.github import pull_requests as provider

    context, person = member_cli.resolve_member_context("aiko")
    context.person = person
    context.integration_factory = SimpleIntegrationFactory()
    context.team.project.services["code_hosting_service"] = {"name": "github"}

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[{"number": 42, "state": "open"}])

    # Let the service own/close the mocked client, just as in production.
    async def get_client(*_args, **_kwargs):
        return httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr(provider, "create_github_client", get_client)
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(
            monkeypatch, tmp_path, access=CommandAccess(read_only=True)
        ) as command:
            result = await WindowCodeHostingService(command.client, "aiko").read(
                "dependency_alerts", "GuildBotics/GuildBotics"
            )
            # The member command's own refusal is a read failure the command
            # reports; the grant's refusal of another person is not.
            for person, resource, refusal in [
                ("other", "dependency_alerts", MemberCommandError),
                ("aiko", "secrets", RepositoryReadError),
            ]:
                with pytest.raises(refusal):
                    await WindowCodeHostingService(command.client, person).read(
                        resource, "GuildBotics/GuildBotics"
                    )
            refused = await command.client.acall(
                "member",
                arguments=[
                    "repository",
                    "read",
                    "--person",
                    "aiko",
                    "--resource",
                    "dependency_alerts",
                    "--repo",
                    "GuildBotics/GuildBotics",
                    "--method",
                    "PATCH",
                ],
                stdin="",
            )
            assert refused["exit_code"] != 0
    finally:
        lease.release()
    assert result.items[0].id == "42"
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
async def test_a_turn_the_command_left_running_is_ended_and_not_resumed(
    tmp_path, monkeypatch
):
    """A command stopped mid-turn never ends its turn itself: its grant ends
    it with the command, and the session it was cut short in is marked so
    that the next turn of that work starts afresh."""
    conversations = ConversationStore(get_workspace_root())
    record = conversations.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "session-1"
    conversations.save(record)
    lease = PersonExecutionLease("aiko")
    lease.acquire(source="manual", command="test", work_id="work")
    try:
        async with _command(monkeypatch, tmp_path) as command:
            await _begin(command)
            assert lease.metadata.run_id == _RUN
        await command.grant.close()
        await command.grant.close()
        run_id = lease.metadata.run_id
    finally:
        lease.release()

    saved = conversations.load(_key())
    assert saved is not None
    assert (saved.healthy, saved.rotation_reason) == (False, "cancelled")
    assert run_id == ""


@pytest.mark.asyncio
async def test_cancelled_save_finishes_before_close_marks_the_conversation(
    tmp_path, monkeypatch
):
    """The broker settles the save's thread before close marks its session."""
    started, release, finished = (threading.Event() for _ in range(3))
    conversations = ConversationStore(get_workspace_root())
    record = conversations.resolve(_key(), ResumePolicy.AUTO)
    record.provider_session_id = "session-1"
    conversations.save(record)
    save = ConversationStore.save

    def slow_save(store, saved):
        if saved.healthy:
            started.set()
            assert release.wait(10)
        try:
            save(store, saved)
        finally:
            if saved.healthy:
                finished.set()

    monkeypatch.setattr(ConversationStore, "save", slow_save)
    async with _command(monkeypatch, tmp_path) as command:
        await _begin(command)
        broker = environment.running_command()._broker
        saving = asyncio.create_task(command.grant.save(record))
        # The same host task the broker tracks for a save call.
        broker._calls.add(saving)
        saving.add_done_callback(broker._calls.discard)
        closing = None
        try:
            assert await asyncio.to_thread(started.wait, 5)

            async def close():
                await broker.settle(abandon=True)
                await command.grant.close()

            closing = asyncio.create_task(close())
            await asyncio.sleep(0.1)
            ended_early = closing.done()
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 5)
            await asyncio.to_thread(finished.wait, 5)
        with pytest.raises(asyncio.CancelledError):
            await saving

    saved = conversations.load(_key())
    assert saved is not None
    assert (saved.healthy, saved.rotation_reason) == (False, "cancelled")
    assert not ended_early


async def _wait_for_task_event(event: asyncio.Event, task: asyncio.Task[Any]) -> None:
    """Wait for a checkpoint while surfacing the producing task's failure."""
    waiting = asyncio.create_task(event.wait())
    try:
        await asyncio.wait({waiting, task}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        assert event.is_set(), "Task finished before reaching the checkpoint"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "host"])
@pytest.mark.parametrize("normal_exit", [False, True])
async def test_cancelled_command_waits_for_member_write_before_discarding_vm(
    tmp_path, monkeypatch, transport, normal_exit
):
    """Command cancellation settles both member transports before teardown."""
    started, ready = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    posted = tmp_path / "posted.txt"
    active: list[Any] = []
    pending: list[asyncio.Task[Any]] = []
    closed_at_write: list[bool] = []

    def run_in_process(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        closed_at_write.append(_Booted.booted[0].closed)
        posted.write_text("member write", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(
        importlib.import_module("guildbotics.cli.member"),
        "run_in_process",
        run_in_process,
    )

    async def run_command():
        async with _command(monkeypatch, tmp_path) as command:
            await _begin(command)
            shared = environment.running_command()
            active.append(shared)
            if transport == "mcp":
                call = shared._broker.execute(shared._broker.turn_grant, ["help"])
            else:
                call = command.client.acall("member", arguments=["help"], stdin="")
            pending.append(asyncio.create_task(call))
            await _wait_for_task_event(started, pending[-1])
            if normal_exit:

                async def never():
                    await asyncio.Event().wait()

                waiting = asyncio.create_task(never())
                shared._broker._calls.add(waiting)
                waiting.add_done_callback(shared._broker._calls.discard)
            ready.set()
            if normal_exit:
                return
            await asyncio.Event().wait()

    task = asyncio.create_task(run_command())
    try:
        await _wait_for_task_event(ready, task)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not _Booted.booted[0].closed
        assert not posted.exists()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.gather(*pending, return_exceptions=True)

    assert posted.read_text(encoding="utf-8") == "member write"
    assert closed_at_write == [False]
    assert _Booted.booted[0].closed
    assert active[0]._environment is None
    assert active[0]._broker._server is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["vm", "gateway"])
async def test_cancellation_during_teardown_waits_for_timed_out_member_write(
    tmp_path, monkeypatch, stage
):
    """A timeout remains prompt, but cancellation during teardown drains it."""
    from guildbotics.intelligences.agent_runtime import member_broker

    started, tearing_down, torn_down = (asyncio.Event() for _ in range(3))
    release = threading.Event()
    loop = asyncio.get_running_loop()
    posted = tmp_path / "posted.txt"

    def run_in_process(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        posted.write_text("member write", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(
        importlib.import_module("guildbotics.cli.member"),
        "run_in_process",
        run_in_process,
    )
    monkeypatch.setattr(member_broker, "_COMMAND_TIMEOUT_SECONDS", 0.05)

    async def run_command():
        async with _command(monkeypatch, tmp_path) as command:
            await _begin(command)
            shared = environment.running_command()
            close_broker = shared._broker.close

            async def broker_closed():
                await close_broker()
                torn_down.set()

            monkeypatch.setattr(shared._broker, "close", broker_closed)
            target = (
                _Booted.booted[0]
                if stage == "vm"
                else next(iter(shared._gateways.values()))
            )
            close = target.close

            async def slow_close():
                tearing_down.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await close()

            monkeypatch.setattr(target, "close", slow_close)
            result = await shared._broker.execute(shared._broker.turn_grant, ["help"])
            assert result.exit_code == 124

    task = asyncio.create_task(run_command())
    try:
        await _wait_for_task_event(tearing_down, task)
        assert started.is_set()
        task.cancel()
        await _wait_for_task_event(torn_down, task)
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not posted.exists()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert posted.read_text(encoding="utf-8") == "member write"
    assert _Booted.booted[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_test, arguments, failure_at",
    [
        (
            test_cancelled_command_waits_for_member_write_before_discarding_vm,
            {"transport": transport, "normal_exit": normal_exit},
            failure_at,
        )
        for failure_at in ("begin", "member")
        for transport in ("mcp", "host")
        for normal_exit in (False, True)
    ]
    + [
        (
            test_cancellation_during_teardown_waits_for_timed_out_member_write,
            {"stage": stage},
            failure_at,
        )
        for failure_at in ("begin", "close")
        for stage in ("vm", "gateway")
    ],
)
async def test_member_teardown_tests_report_checkpoint_failure(
    tmp_path, monkeypatch, run_test, arguments, failure_at
):
    """All four checkpoints surface producer failures without hanging."""
    module = sys.modules[__name__]
    original_begin = _begin
    failed = asyncio.Event()

    @asynccontextmanager
    async def command(*_args, **_kwargs):
        yield None

    failure = RuntimeError("init broke")

    async def fail(*_args, **_kwargs):
        failed.set()
        raise failure

    async def begin(command, *args, **kwargs):
        if failure_at == "begin":
            return await fail()
        result = await original_begin(command, *args, **kwargs)
        broker = environment.running_command()._broker
        if failure_at == "member":
            monkeypatch.setattr(broker, "execute", fail)
            monkeypatch.setattr(command.client, "acall", fail)
        else:
            close = broker.close

            async def close_then_fail():
                await close()
                await fail()

            monkeypatch.setattr(broker, "close", close_then_fail)
        return result

    if failure_at == "begin":
        monkeypatch.setattr(module, "_command", command)
    monkeypatch.setattr(module, "_begin", begin)
    before = asyncio.all_tasks()

    async def check():
        with pytest.raises(RuntimeError) as caught:
            await run_test(tmp_path, monkeypatch, **arguments)
        assert caught.value is failure

    checking = asyncio.create_task(check())
    try:
        await _wait_for_task_event(failed, checking)
        # Startup is outside this bound; real command cleanup needs more time.
        done, _ = await asyncio.wait(
            {checking}, timeout=1 if failure_at == "begin" else 15
        )
        assert checking in done, "Checkpoint failure was not reported"
        await checking
    finally:
        checking.cancel()
        await asyncio.gather(checking, return_exceptions=True)
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        "save",
        "mark_unhealthy",
        "record_completed",
        "record_completion_missing",
        "record",
        "close",
    ],
)
async def test_host_state_writes_finish_before_cancellation_returns(
    tmp_path, monkeypatch, call
):
    """All host state writers retain their worker through repeated cancellation."""
    started = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    conversations = ConversationStore(get_workspace_root())
    record = conversations.resolve(_key(), ResumePolicy.AUTO)
    conversations.save(record)

    async with _command(monkeypatch, tmp_path) as command:
        targets = {
            "save": (command.grant._conversations, "save", {"record": record}),
            "mark_unhealthy": (
                command.grant._conversations,
                "mark_unhealthy",
                {"record": record, "reason": "cancelled"},
            ),
            "record_completed": (
                command.ledger,
                "record_completed",
                {"run_id": _RUN, "attempt": 1},
            ),
            "record_completion_missing": (
                command.ledger,
                "record_completion_missing",
                {"run_id": _RUN, "attempt": 1, "max_attempts": 3, "error": "missing"},
            ),
            "record": (command.grant, "_write", {"entries": []}),
            "close": (command.grant._conversations, "mark_unhealthy", {}),
        }
        if call == "close":
            await _begin(command)
        target, method, arguments = targets[call]
        original = getattr(target, method)

        def write(*args):
            loop.call_soon_threadsafe(started.set)
            try:
                assert release.wait(10)
                return original(*args)
            finally:
                finished.set()

        monkeypatch.setattr(target, method, write)
        task = asyncio.create_task(getattr(command.grant, call)(**arguments))
        try:
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        assert finished.is_set()


@pytest.mark.asyncio
async def test_a_conversation_that_cannot_be_marked_leaves_the_command_its_end(
    tmp_path, monkeypatch, caplog
):
    """The grant still ends the turn the command left running; what it
    could not mark of the conversation is logged, not what the command ends
    with."""
    async with _command(monkeypatch, tmp_path) as command:
        await _begin(command)

    def unreadable(*_):
        raise OSError("the ledger is unreadable")

    monkeypatch.setattr(ConversationStore, "load", unreadable)
    with caplog.at_level(logging.ERROR, logger="guildbotics"):
        await command.grant.close()

    assert "the ledger is unreadable" in caplog.text
    assert command.grant._turn is None


@pytest.mark.asyncio
async def test_a_command_that_did_not_end_well_stops_the_calls_it_made(
    tmp_path, monkeypatch
):
    """What a stopped or failed command asked for is no longer wanted: the
    calls being answered are stopped rather than waited for."""
    lending = asyncio.Event()

    async def never(*_args: Any, **_kwargs: Any) -> Any:
        lending.set()
        await asyncio.Event().wait()

    turn: asyncio.Task[Any] | None = None
    with pytest.raises(RuntimeError, match="the command failed"):
        async with _command(monkeypatch, tmp_path) as command:
            monkeypatch.setattr(environment, "_lend", never)
            turn = asyncio.create_task(_begin(command))
            await asyncio.wait_for(lending.wait(), 5)
            raise RuntimeError("the command failed")

    assert turn is not None
    with pytest.raises(HostCallError):
        await asyncio.wait_for(turn, 5)
