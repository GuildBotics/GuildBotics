"""The command's window to the host, reached the way its microVM reaches it:
the client and the real window in one process, over the broker's server."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpecError,
    guest_path,
    host_path,
)
from guildbotics.intelligences.agent_runtime import environment
from guildbotics.intelligences.agent_runtime.host_client import (
    COMMAND_ENV,
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    ClientConversationStore,
    ClientRunLedger,
    CommandFacts,
    EventEntry,
    HostCallError,
    HostClient,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.agent_runtime.host_window import HostWindow
from guildbotics.intelligences.agent_runtime.member_broker import (
    MEMBER_BROKER_TOKEN_ENV,
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
from guildbotics.observability import SpanContext, trace_scope
from guildbotics.observability import diagnostics_events
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    get_workspace_root,
)
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.intelligences.agent_runtime.contract_doubles import (
    command_at,
    settle_contract,
)
from tests.guildbotics.intelligences.agent_runtime.test_environment import (
    _Booted,
    _device,
)

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


@asynccontextmanager
async def _command(
    monkeypatch,
    tmp_path,
    *tools: str,
    work_kind: str = "",
    access: CommandAccess = CommandAccess(),
) -> AsyncIterator[_Command]:
    """A command of aiko in ``repository`` of the test's workspace, running
    ``tools`` (Claude Code by default, logged in), for the run ``run-1``,
    in a trace of the scheduler's: its window, and the client its microVM
    reaches it with."""
    tools = tools or ("claude",)
    _device(monkeypatch, tmp_path, *tools)
    ledger = _Ledger()
    repository = get_workspace_root() / "repository"
    grant = HostWindow(
        "aiko", _RUN, work_kind, workspace_root=get_workspace_root(), ledger=ledger
    )
    with trace_scope(
        "scheduler", person_id="aiko", attributes={"service_run_id": "svc-1"}
    ) as trace:
        async with command_at(repository, tools, access, host=grant):
            endpoint = await environment.running_command().window()
            yield _Command(
                HostClient(endpoint.host_url, endpoint.token),
                ledger,
                repository,
                trace.trace_id,
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
            assert turn.member_server == broker.mcp_server
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
        async with _command(monkeypatch, tmp_path) as command:
            _device(monkeypatch, tmp_path, "claude", "codex")
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

    assert refused.value.category == "refused"
    assert _Booted.booted == []


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
    at once."""
    span = SpanContext("span-1", "call-1", "parent-1", "cli_agent")
    async with _command(monkeypatch, tmp_path) as command:
        await asyncio.to_thread(
            command.client.record,
            [
                IoEntry(
                    span=span, io_type="cli_agent.request", payload={"prompt": "p"}
                ),
                EventEntry(
                    span=span,
                    conversation=_key(),
                    generation=2,
                    context_cursor="7",
                    event=AgentEvent(AgentEventKind.TURN, "started"),
                ),
                IoEntry(
                    span=span,
                    io_type="cli_agent.response",
                    payload={"stdout": "done", "stderr": "e" * 70_000},
                ),
                SummaryEntry(span=span, slot="default", status="finished", model="m"),
            ],
        )

    assert [(record["kind"], record["type"]) for record in written] == [
        ("io", "cli_agent.request"),
        ("event", "agent_runtime.turn"),
        ("io", "cli_agent.response"),
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
    # The transcript keeps what its detail says of a response's stderr.
    assert written[2]["payload"]["stderr_truncated"] is True
    assert written[3]["attributes"]["agent.slot"] == "default"


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

    assert refused.value.category == "refused"
    assert command.ledger.calls == [] and written == []
    assert _Booted.booted == []


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
        endpoint = await shared.window()
        variables = shared._broker._host.variables(endpoint)

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
    )
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


def test_a_guest_path_is_taken_back_to_the_host_path_it_spells(tmp_path) -> None:
    for path in (tmp_path, tmp_path / "a b" / "c"):
        assert host_path(guest_path(path)) == path
    for guest in ("", "relative", "/a/../b", "/a/./b", "/a//b", "/a/"):
        with pytest.raises(AgentEnvironmentSpecError):
            host_path(guest)


def test_the_client_loads_nothing_only_the_host_may_hold() -> None:
    """It runs inside the command's microVM, beside the command's machinery."""
    probe = (
        "import json, sys\n"
        "import guildbotics.intelligences.agent_runtime.host_client\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    loaded = json.loads(
        subprocess.run(
            [sys.executable, "-c", probe], check=True, capture_output=True, text=True
        ).stdout
    )
    host_only = {
        "keyring",
        "mcp",
        "microsandbox",
        "starlette",
        "uvicorn",
        "guildbotics.intelligences.agent_runtime.environment",
        "guildbotics.intelligences.agent_runtime.member_broker",
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
    """A turn still starting when the command ends starts, and its microVM
    is discarded with the command, not left running beside it."""
    booting, boot = asyncio.Event(), asyncio.Event()
    async with _command(monkeypatch, tmp_path) as command:
        start = environment._start

        async def slow(*args: Any, **kwargs: Any) -> Any:
            booting.set()
            await boot.wait()
            return await start(*args, **kwargs)

        monkeypatch.setattr(environment, "_start", slow)
        turn = asyncio.create_task(_begin(command))
        waiting = asyncio.create_task(booting.wait())
        await asyncio.wait({turn, waiting}, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()
        assert booting.is_set(), turn.exception()
        asyncio.get_running_loop().call_later(0.2, boot.set)

    assert (await turn).turn_grant
    [booted] = _Booted.booted
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
        return "x" * 128

    async with _served(host) as (client, url):
        slow = HostClient(url, client.headers["Authorization"].split()[1])
        with pytest.raises(HostCallError) as timed_out:
            await slow.acall("slow")
        with pytest.raises(HostCallError) as too_much:
            await slow.acall("large")

    assert (timed_out.value.category, str(timed_out.value)) == (
        "failed",
        "The host call timed out.",
    )
    assert (too_much.value.category, str(too_much.value)) == (
        "failed",
        "The call's result is too large.",
    )
