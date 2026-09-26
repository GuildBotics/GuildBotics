from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_runtime import environment, windows_job


class _Process:
    def __init__(self, *, pid: int = 42, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            await asyncio.Event().wait()
        return int(self.returncode)

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1

    def terminate(self) -> None:
        pytest.fail("Windows graceful shutdown must not call Process.terminate()")


@pytest.mark.asyncio
async def test_create_agent_subprocess_assigns_before_resume_on_windows(
    monkeypatch,
) -> None:
    events: list[object] = []
    process = _Process()

    class Job:
        @staticmethod
        def create():
            events.append("create-job")
            return Job()

        def assign_and_resume(self, pid: int) -> None:
            events.append(("assign-resume", pid))

        def close(self) -> None:
            events.append("close")

    async def create_process(*program: str, **kwargs: Any):
        events.append(("spawn", program, kwargs))
        return process

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "WindowsJob", Job)
    monkeypatch.setattr(environment.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        environment,
        "register_process_job",
        lambda registered, job: events.append(("register", registered, job)),
    )

    created = await environment.create_agent_subprocess("agent", "run", stdin=-1)

    assert created is process
    assert events[0] == "create-job"
    assert events[1][0] == "spawn"
    assert events[1][2]["creationflags"] == windows_job.creation_flags()
    assert events[2] == ("assign-resume", 42)
    assert events[3][0] == "register"


@pytest.mark.asyncio
async def test_create_agent_subprocess_recovers_suspended_process_on_failure(
    monkeypatch,
) -> None:
    process = _Process()
    events: list[str] = []

    class Job:
        @staticmethod
        def create():
            return Job()

        def assign_and_resume(self, _pid: int) -> None:
            raise OSError("assign failed")

        def terminate(self) -> None:
            events.append("terminate-job")

        def close(self) -> None:
            events.append("close-job")

    async def create_process(*_program: str, **_kwargs: Any):
        return process

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "WindowsJob", Job)
    monkeypatch.setattr(environment.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(OSError, match="assign failed"):
        await environment.create_agent_subprocess("agent")

    assert events == ["terminate-job", "close-job"]
    assert process.killed is True


@pytest.mark.asyncio
async def test_windows_tree_shutdown_waits_then_terminates_job(monkeypatch) -> None:
    process = _Process()
    calls: list[object] = []

    def terminate_job(owned_process) -> bool:
        calls.append(owned_process)
        process.returncode = 1
        return True

    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(environment, "terminate_process_job", terminate_job)

    await environment.terminate_process_tree(process, grace_seconds=0)

    assert calls == [process]


@pytest.mark.asyncio
async def test_windows_tree_shutdown_terminates_descendants_after_root_exit(
    monkeypatch,
) -> None:
    process = _Process(returncode=0)
    calls: list[object] = []
    monkeypatch.setattr(environment, "_WINDOWS", True)
    monkeypatch.setattr(
        environment,
        "terminate_process_job",
        lambda owned_process: calls.append(owned_process) or True,
    )

    await environment.terminate_process_tree(process)

    assert calls == [process]


@pytest.mark.asyncio
async def test_posix_tree_shutdown_escalates_process_group(monkeypatch) -> None:
    process = _Process()
    calls: list[tuple[int, bool]] = []

    def terminate_group(pid: int, *, force: bool = False) -> None:
        calls.append((pid, force))
        if force:
            process.returncode = 1

    monkeypatch.setattr(environment, "_WINDOWS", False)
    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment, "terminate_posix_process_group", terminate_group)

    await environment.terminate_process_tree(process, grace_seconds=0)

    assert calls == [(42, False), (42, True)]


def test_windows_job_assigns_process_then_resumes_only_thread(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        windows_job,
        "_open_process_for_job",
        lambda pid: events.append(("open", pid)) or 8,
    )
    monkeypatch.setattr(
        windows_job,
        "_assign_process",
        lambda job, process: events.append(("assign", job, process)),
    )
    monkeypatch.setattr(
        windows_job, "_close_handle", lambda handle: events.append(("close", handle))
    )
    monkeypatch.setattr(windows_job, "_thread_ids_for", lambda pid: [pid + 1])
    monkeypatch.setattr(
        windows_job, "_resume_thread", lambda thread: events.append(("resume", thread))
    )

    windows_job.WindowsJob(7).assign_and_resume(42)

    assert events == [
        ("open", 42),
        ("assign", 7, 8),
        ("close", 8),
        ("resume", 43),
    ]


def test_agent_runtime_has_one_subprocess_creation_boundary() -> None:
    runtime_dir = Path(environment.__file__).parent
    direct_calls = {
        path.name: path.read_text(encoding="utf-8").count(
            "asyncio.create_subprocess_exec"
        )
        for path in runtime_dir.glob("*.py")
    }

    assert direct_calls == {
        name: (2 if name == "environment.py" else 0) for name in direct_calls
    }


def test_no_adapter_decides_what_a_read_only_turn_may_do() -> None:
    """A read-only turn is held by its environment, the same for every
    provider; an adapter that narrowed it for its own provider would make the
    guarantee differ between providers again."""
    runtime_dir = Path(environment.__file__).parent
    deciding = [
        path.name
        for path in runtime_dir.glob("*.py")
        if path.name != "environment.py"
        and re.search(r"\.read_only\b", path.read_text(encoding="utf-8"))
    ]

    assert deciding == []


class _Relay:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"ready\n")
        self.killed = False

    async def kill(self) -> None:
        self.killed = True


class _Booted:
    """A microVM as the runtime boots it: its files, what runs in it, and
    whether it was stopped."""

    booted: list[_Booted] = []

    def __init__(self, spec, before_stop) -> None:
        self.spec = spec
        self.before_stop = before_stop
        self.files: dict[str, bytes] = {
            "/etc/ssl/certs/ca-certificates.crt": b"SYSTEM-CAS",
            "/etc/hosts": b"127.0.0.1 localhost",
        }
        self.relays: list[tuple[tuple[str, ...], _Relay]] = []
        self.closed = False
        _Booted.booted.append(self)

    async def write_file(self, path, data):
        self.files[path] = data

    async def read_file(self, path):
        return self.files.get(path)

    async def run(self, *command, limit, **_):
        relay = _Relay()
        self.relays.append((command, relay))
        return relay

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.before_stop(self)


def _device(monkeypatch, tmp_path, *logins: str, where=None):
    """This device, able to run every tool, with ``logins`` logged in: the
    microVMs it boots are recorded in :attr:`_Booted.booted`."""
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.credential_vault import (
        CredentialVaultError,
    )

    where = where or environment.LoginEnvironment(
        tmp_path / "snapshot", 1024, 1, ("1.1.1.1",)
    )
    monkeypatch.setattr(
        environment, "_ready", lambda name: (environment.cli_agent_info(name), where)
    )

    def unsealed(tool):
        if tool.name not in logins:
            raise CredentialVaultError("missing")
        return {tool.provision.auth: json.dumps(_LOGINS[tool.name]).encode()}

    monkeypatch.setattr(provider_state, "_unsealed_login", unsealed)
    _Booted.booted = []

    async def start(spec, at, *, before_stop):
        assert at is where
        return _Booted(spec, before_stop)

    monkeypatch.setattr(environment, "_start", start)
    return where


def _turn(tmp_path, tool_name="claude", **overrides: Any):
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    return AgentExecutionContext(
        **{
            "person_id": "aiko",
            "run_id": "turn",
            "cwd": tmp_path / "repository",
            "workspace_root": tmp_path,
            "workspace_data_root": tmp_path,
            "conversation_key": ConversationKey("aiko", tool_name, "manual", "turn"),
            **overrides,
        }
    )


async def _claude_turn_spec(tmp_path, monkeypatch, context):
    """The spec a Claude Code turn run in ``context`` boots with."""
    async with environment.command_environment(CommandAccess()):
        _device(monkeypatch, tmp_path, "claude")
        turn = await environment.start_turn_environment(context, "claude")
        await turn.close()
        return turn.spec


@pytest.mark.asyncio
async def test_a_read_only_turn_resumes_its_session_but_leaves_no_trace_behind(
    tmp_path, monkeypatch
):
    """Only the read-only turns' own sessions stay writable: nothing of the
    store or cache other turns share is bound, so nothing a read-only turn
    wrote is resumed or run by a later one."""
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.contract import (
        AccessContract,
        NetworkPolicy,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    monkeypatch.setattr(
        provider_state,
        "get_machine_state_path",
        lambda *parts: tmp_path.joinpath("machine", *parts),
    )
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="investigate",
        cwd=tmp_path / "work",
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "claude", "troubleshooting", "c1"),
        contract=AccessContract(
            network=NetworkPolicy(mode="unrestricted"), read_only=True
        ),
    )

    spec = await _claude_turn_spec(tmp_path, monkeypatch, context)

    writable = {mount.host for mount in spec.mounts if not mount.readonly}
    assert writable == {
        provider_state.read_only_state_dir(environment.cli_agent_info("claude"))
        / "projects"
    }
    assert not spec.network.unrestricted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspects", [frozenset(), frozenset({"diagnostics", "config"})]
)
async def test_what_a_turn_inspects_is_mounted_read_only(
    tmp_path, monkeypatch, inspects
):
    """The recorded runs and the configuration are mounted read-only only for
    a turn whose caller lets it inspect them."""
    from guildbotics.intelligences.agent_environment.spec import (
        EnvironmentMount,
        guest_path,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )
    from guildbotics.utils.fileio import get_template_path

    state = tmp_path / ".guildbotics"
    run = state / "local" / "run"
    run.mkdir(parents=True)
    (state / "config").mkdir()
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="investigate",
        cwd=state / "local" / "work" / "troubleshooting",
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "claude", "troubleshooting", "c1"),
        inspects=inspects,
    )
    spec = await _claude_turn_spec(tmp_path, monkeypatch, context)

    mounts = set(spec.mounts)
    expected = {
        EnvironmentMount(guest_path(run), run, True),
        EnvironmentMount(guest_path(state / "config"), state / "config", True),
    }
    if inspects:
        assert expected <= mounts
        # The packaged defaults are named inside the code every microVM has.
        named = environment.inspected_directories(inspects, tmp_path)
        assert named == {
            "diagnostics": guest_path(run),
            "config": guest_path(state / "config"),
            "templates": "/opt/guildbotics/code/guildbotics/templates",
        }
        assert any(
            mount.readonly
            and mount.host is not None
            and (mount.host / "templates") == get_template_path()
            and f"{mount.guest}/templates" == named["templates"]
            for mount in mounts
        )
    else:
        assert not expected & mounts


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_every_turn_has_the_running_code_read_only_apart_from_the_users(
    tmp_path, monkeypatch, read_only
):
    """GuildBotics' own code -- the package this process runs -- is in every
    microVM at a place of its own, read-only, so a turn working in the
    checkout it runs from still writes the package there."""
    import guildbotics
    from guildbotics.intelligences.agent_environment.contract import AccessContract
    from guildbotics.intelligences.agent_environment.spec import (
        EnvironmentMount,
        guest_path,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    package = Path(guildbotics.__file__).resolve().parent
    checkout = package.parent
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="r1",
        cwd=checkout,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "claude", "manual", "r1"),
        contract=AccessContract(read_only=read_only),
    )
    spec = await _claude_turn_spec(tmp_path, monkeypatch, context)

    code = EnvironmentMount("/opt/guildbotics/code/guildbotics", package, True)
    assert code in spec.mounts
    assert environment.code_path(package / "cli") == f"{code.guest}/cli"
    assert not any(
        mount.guest.startswith(guest_path(package))
        for mount in spec.mounts
        if mount != code
    )
    assert (
        EnvironmentMount(
            guest_path(checkout), None if read_only else checkout, read_only
        )
        in spec.mounts
    )


def test_a_directory_not_there_yet_is_neither_mounted_nor_named(tmp_path):
    """Before the first run there is no run directory: the turn is not told to
    look in a place its environment does not have."""
    from guildbotics.intelligences.agent_environment.spec import guest_path

    (tmp_path / ".guildbotics" / "config").mkdir(parents=True)

    directories = environment.inspected_directories({"diagnostics", "config"}, tmp_path)

    assert "diagnostics" not in directories
    assert directories["config"] == guest_path(tmp_path / ".guildbotics" / "config")


def test_inspecting_is_limited_to_known_scopes(tmp_path):
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    def context(**overrides: Any) -> AgentExecutionContext:
        return AgentExecutionContext(
            person_id="aiko",
            run_id="r1",
            cwd=tmp_path,
            workspace_root=tmp_path,
            workspace_data_root=tmp_path,
            conversation_key=ConversationKey("aiko", "claude", "manual", "r1"),
            **overrides,
        )

    with pytest.raises(ValueError, match="Unknown inspection scopes"):
        context(inspects=frozenset({"secrets"}))


def _jwt(claims: dict[str, object]) -> str:
    def segment(value: object) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode())
        return encoded.rstrip(b"=").decode()

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.U0lHTkFUVVJF"


#: Codex's tokens are JWTs; what they claim is as secret as they are.
_CODEX_ACCESS = _jwt({"exp": 4102444800, "secret": "REAL-SYNTHETIC-459"})
_CODEX_ID = _jwt({"email": "a@example.com", "secret": "REAL-SYNTHETIC-459"})
#: A synthetic login of each brokered tool, as it is sealed.
_LOGINS = {
    "antigravity": {
        "token": {
            "access_token": "REAL-SYNTHETIC-459",
            "token_type": "Bearer",
            "refresh_token": "REFRESH-SYNTHETIC-459",
            "expiry": "2100-01-01T00:00:00.123456789Z",
        },
        "auth_method": "consumer",
        "id_token": "REAL-SYNTHETIC-459-ID",
    },
    "codex": {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _CODEX_ID,
            "access_token": _CODEX_ACCESS,
            "refresh_token": "REFRESH-SYNTHETIC-459",
            "account_id": "account-459",
        },
        "last_refresh": "2026-09-23T00:00:00Z",
    },
    "claude": {
        "claudeAiOauth": {
            "accessToken": "REAL-SYNTHETIC-459",
            "refreshToken": "REFRESH-SYNTHETIC-459",
            "expiresAt": 4102444800000,
        }
    },
    "copilot": {
        "authTokens": {
            "https://github.com:synthetic459": {"token": "REAL-SYNTHETIC-459"}
        }
    },
    "grok": {
        "https://auth.x.ai::00000000-0000-0000-0000-000000000459": {
            "key": "REAL-SYNTHETIC-459",
            "refresh_token": "REFRESH-SYNTHETIC-459",
            "expires_at": "2100-01-01T00:00:00Z",
        }
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_LOGINS))
async def test_a_brokered_turn_reaches_its_api_only_through_its_gateway(
    tmp_path, monkeypatch, tool_name
):
    """The turn holds the stand-in -- as a file, or as the command a tool
    takes its login from -- is pointed at the gateway, reaches none of the
    provider's domains itself, and the stand-in opens nothing once the
    microVM is gone. A gateway that answers over TLS is trusted by the turn,
    as the names it answers for, and a host the tool fixes is relayed to it."""
    import ssl
    from functools import reduce

    import httpx

    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.contract import AccessContract
    from guildbotics.intelligences.agent_environment.spec import (
        GUEST_HOST_ALIAS,
        guest_home,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    tool = environment.cli_agent_info(tool_name)
    broker = tool.provision.credential_broker
    assert broker is not None
    # The lending as it is, over a login that is not read from a vault.
    _device(monkeypatch, tmp_path, tool_name)
    context = _turn(tmp_path, tool_name, contract=AccessContract())

    running = AsyncExitStack()
    await running.enter_async_context(environment.command_environment(CommandAccess()))
    turn = await environment.start_turn_environment(
        context, tool_name, env={"IS_SANDBOX": "1"}
    )
    (booted,) = _Booted.booted

    spec = turn.spec
    scheme = "https" if broker.tls else "http"
    (base_url,) = {spec.env[variable] for variable in broker.base_url_env}
    port = int(base_url.removesuffix(broker.base_url_path).rsplit(":", 1)[1])
    assert base_url == f"{scheme}://{GUEST_HOST_ALIAS}:{port}{broker.base_url_path}"
    assert spec.network.host_ports == (turn.broker.endpoint.port, port)
    assert set(spec.network.domains) == set(broker.turn_domains)
    assert spec.env["IS_SANDBOX"] == "1"
    held = b"".join(booted.files.values()) + json.dumps(dict(spec.env)).encode()
    for secret in (
        "REAL-SYNTHETIC-459",
        "REFRESH-SYNTHETIC-459",
        "REAL-SYNTHETIC-459-ID",
        _CODEX_ACCESS,
        _CODEX_ID,
    ):
        assert secret.encode() not in held
    auth = f"{guest_home()}/{tool.provision.state_root}/{tool.provision.auth}"
    if broker.stand_in_env:
        assert auth not in booted.files
        stand_in = spec.env[broker.stand_in_env]
    elif broker.stand_in_command_env:
        assert auth not in booted.files
        command = spec.env[broker.stand_in_command_env]
        stand_in = json.loads(command.removeprefix("echo '").removesuffix("'"))[
            "access_token"
        ]
    else:
        stand_in = reduce(
            lambda at, key: at[key],
            broker.access_token,
            json.loads(booted.files[auth]),
        )
    names = [GUEST_HOST_ALIAS]
    verify: ssl.SSLContext | bool = False
    if broker.tls:
        trusted = booted.files[spec.env["SSL_CERT_FILE"]]
        assert trusted.startswith(b"SYSTEM-CAS\n")
        verify = ssl.create_default_context(cadata=trusted.split(b"\n", 1)[1].decode())
        names.extend(broker.relayed_hosts)
    else:
        assert "SSL_CERT_FILE" not in spec.env
    if broker.relayed_hosts:
        ((command, relay),) = booted.relays
        assert command[:2] == ("node", "-e")
        assert command[-2:] == (GUEST_HOST_ALIAS, str(port))
        for host in broker.relayed_hosts:
            assert f"127.0.0.2 {host}".encode() in booted.files["/etc/hosts"]
    else:
        assert booted.relays == []
    assert all(
        mount.host is None or not str(mount.host).endswith(tool.provision.auth)
        for mount in spec.mounts
    )
    # A connection of its own per name, so each is its own TLS handshake.
    fresh = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(verify=verify, limits=fresh) as guest:
        for name in names:  # Each name it answers for, as the turn trusts it.
            answer = await guest.post(
                f"{scheme}://127.0.0.1:{port}/v1/oauth/token",
                headers={"authorization": f"Bearer {stand_in}"},
                extensions={"sni_hostname": name},
            )
            assert answer.status_code == 403

        await turn.close()
        # The command ends, and its microVM and gateways with it.
        await running.aclose()
        assert booted.closed
        assert all(relay.killed for _, relay in booted.relays)
        with pytest.raises(httpx.HTTPError):
            await guest.post(
                f"{scheme}://127.0.0.1:{port}/v1/messages",
                headers={"authorization": f"Bearer {stand_in}"},
            )


@pytest.mark.asyncio
async def test_a_relay_that_does_not_start_stops_the_turn(monkeypatch) -> None:
    """A tool whose host cannot be relayed would reach nothing it needs, or
    the host itself with the stand-in; neither is a turn."""
    from guildbotics.intelligences.agent_environment.runtime import (
        AgentEnvironmentError,
    )
    from guildbotics.utils.i18n_tool import t

    monkeypatch.setattr(environment, "_RELAY_SECONDS", 0.05)

    class Silent:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.killed = False

        async def kill(self) -> None:
            self.killed = True

    class Guest:
        def __init__(self) -> None:
            self.relay = Silent()

        async def read_file(self, path):
            return None

        async def write_file(self, path, data):
            pass

        async def run(self, *command, limit):
            return self.relay

    guest = Guest()
    with pytest.raises(AgentEnvironmentError) as refused:
        await environment._relay(guest, ("www.example.test",), 1234)

    assert str(refused.value) == t(
        "intelligences.agent_environment.runtime.relay_failed"
    )
    assert guest.relay.killed


@pytest.mark.asyncio
async def test_an_image_without_system_cas_stops_the_turn() -> None:
    """The turn CA alone would fail every other TLS the turn makes."""
    from guildbotics.intelligences.agent_environment.runtime import (
        AgentEnvironmentError,
    )
    from guildbotics.utils.i18n_tool import t

    class Guest:
        def __init__(self) -> None:
            self.written: dict[str, bytes] = {}

        async def read_file(self, path):
            return None

        async def write_file(self, path, data):
            self.written[path] = data

    guest = Guest()
    with pytest.raises(AgentEnvironmentError) as refused:
        await environment._trust(guest, b"TURN-CA")

    assert str(refused.value) == t(
        "intelligences.agent_environment.runtime.no_system_cas",
        path="/etc/ssl/certs/ca-certificates.crt",
    )
    assert guest.written == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("refreshes", [True, False])
async def test_a_login_due_for_refresh_is_refreshed_before_the_turn_starts(
    tmp_path, monkeypatch, refreshes
):
    """A tool may give up on its API sooner than a refresh takes, so the
    first request never waits for one; a refresh that fails starts no turn."""
    async with environment.command_environment(CommandAccess()):
        from guildbotics.intelligences.agent_environment import provider_state
        from guildbotics.intelligences.agent_environment.auth_gateway import (
            CredentialUnavailableError,
        )
        from guildbotics.intelligences.agent_environment.contract import AccessContract
        from guildbotics.intelligences.agent_runtime.models import (
            AgentRuntimeError,
            AgentRuntimeErrorCategory,
        )

        tool = environment.cli_agent_info("claude")
        _device(monkeypatch, tmp_path)
        due = {"claudeAiOauth": {**_LOGINS["claude"]["claudeAiOauth"], "expiresAt": 0}}
        sealed = {tool.provision.auth: json.dumps(due).encode()}
        monkeypatch.setattr(provider_state, "_unsealed_login", lambda _: sealed)
        happened: list[str] = []

        async def refresh(selected, at, stale):
            happened.append("refresh")
            if not refreshes:
                raise CredentialUnavailableError("log in again")
            return {tool.provision.auth: json.dumps(_LOGINS["claude"]).encode()}

        monkeypatch.setattr(provider_state, "refresh_login", refresh)
        booting = environment._start

        async def start(spec, at, *, before_stop):
            happened.append("start")
            return await booting(spec, at, before_stop=before_stop)

        monkeypatch.setattr(environment, "_start", start)
        context = _turn(tmp_path, contract=AccessContract())

        if refreshes:
            turn = await environment.start_turn_environment(context, "claude")
            await turn.close()
            assert happened == ["refresh", "start"]
        else:
            with pytest.raises(AgentRuntimeError) as refused:
                await environment.start_turn_environment(context, "claude")
            assert refused.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
            assert str(refused.value) == "log in again"
            assert happened == ["refresh"]


@pytest.mark.asyncio
async def test_a_login_the_gateway_could_not_use_is_told_to_the_turn(
    tmp_path, monkeypatch
):
    """What the tool makes of a refused login is its own affair; the turn
    learns it from the login it lent."""
    async with environment.command_environment(CommandAccess()):
        import httpx

        from guildbotics.intelligences.agent_environment import provider_state
        from guildbotics.intelligences.agent_environment.auth_gateway import (
            CredentialGateway,
        )
        from guildbotics.intelligences.agent_environment.contract import AccessContract
        from guildbotics.utils.i18n_tool import t

        tool = environment.cli_agent_info("copilot")
        _device(monkeypatch, tmp_path, "copilot")
        stand_ins: list[str] = []

        class Revoked(CredentialGateway):
            def __init__(self, *args, **kwargs):
                refuse = httpx.MockTransport(lambda _: httpx.Response(401))
                super().__init__(*args, transport=refuse, **kwargs)

            def lend(self, tokens, stand_in):
                stand_ins.append(stand_in)
                super().lend(tokens, stand_in)

        monkeypatch.setattr(environment, "CredentialGateway", Revoked)
        context = _turn(tmp_path, "copilot", contract=AccessContract())
        turn = await environment.start_turn_environment(context, "copilot")
        (base_url,) = {
            turn.spec.env[variable]
            for variable in tool.provision.credential_broker.base_url_env
        }
        port = base_url.rsplit(":", 1)[1].split("/", 1)[0]
        try:
            assert context.login.refusal() == ""
            async with httpx.AsyncClient() as guest:
                await guest.get(
                    f"http://127.0.0.1:{port}/models",
                    headers={"authorization": f"Bearer {stand_ins[-1]}"},
                )
        finally:
            await turn.close()

        assert context.login.refusal() == t(
            "intelligences.agent_environment.tool.login_refused",
            tool=tool.label,
            command=provider_state.login_command("copilot"),
        )


def _state_root(tool_name: str) -> str:
    from guildbotics.intelligences.agent_environment.spec import guest_home

    tool = environment.cli_agent_info(tool_name)
    return f"{guest_home()}/{tool.provision.state_root}"


@pytest.mark.asyncio
async def test_the_turns_of_a_command_share_one_microvm_until_the_command_ends(
    tmp_path, monkeypatch
):
    """The microVM boots before the first turn, able to run every tool the
    member is configured with, and each turn works where it was asked to;
    only the command's end discards it."""
    from guildbotics.intelligences.agent_environment.spec import guest_path

    _device(monkeypatch, tmp_path, "claude", "codex")
    tools = frozenset({"claude", "codex"})
    repository = tmp_path / "repository"

    async with environment.command_environment(CommandAccess()):
        first = await environment.start_turn_environment(
            _turn(tmp_path, "claude", tools=tools), "claude"
        )
        await first.close()
        second = await environment.start_turn_environment(
            _turn(tmp_path, "codex", tools=tools, cwd=repository / "package"),
            "codex",
        )
        await second.close()
        (booted,) = _Booted.booted
        assert not booted.closed

    assert booted.closed
    assert booted.spec.cwd == guest_path(repository)
    assert first.spec.cwd == guest_path(repository)
    assert second.spec.cwd == guest_path(repository / "package")
    # The broker's port and a gateway for each tool, opened at the boot.
    assert len(booted.spec.network.host_ports) == 3
    mounted = [mount.guest for mount in booted.spec.mounts]
    for tool_name in tools:
        assert any(guest.startswith(_state_root(tool_name)) for guest in mounted)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "cancellation"])
async def test_a_command_that_does_not_end_well_still_discards_its_microvm(
    tmp_path, monkeypatch, ending
):
    _device(monkeypatch, tmp_path, "claude")
    started = asyncio.Event()

    async def command() -> None:
        async with environment.command_environment(CommandAccess()):
            await environment.start_turn_environment(_turn(tmp_path), "claude")
            started.set()
            if ending == "failure":
                raise RuntimeError("the command failed")
            await asyncio.Event().wait()

    task = asyncio.create_task(command())
    await started.wait()
    if ending == "cancellation":
        task.cancel()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task

    (booted,) = _Booted.booted
    assert booted.closed


@pytest.mark.asyncio
async def test_no_turn_runs_outside_a_command(tmp_path, monkeypatch):
    """Every turn runs in the environment of the command it belongs to."""
    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
    )

    _device(monkeypatch, tmp_path, "claude")

    with pytest.raises(AgentRuntimeError) as refused:
        await environment.start_turn_environment(_turn(tmp_path), "claude")

    assert refused.value.category is AgentRuntimeErrorCategory.CONFIGURATION
    assert _Booted.booted == []


def test_only_where_a_command_runs_is_a_command_environment_opened() -> None:
    """No caller runs an AI CLI turn outside a command.

    A turn refuses to start outside a command's environment, so the places
    that open one are the population of places a turn can run from. Each must
    be where a command runs; a caller that opened one of its own would run
    its turns outside any command.
    """
    import ast

    import guildbotics

    package = Path(guildbotics.__file__).parent
    openers: set[tuple[str, str]] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing = {
            id(node): function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
                == "command_environment"
            ):
                module = path.relative_to(package.parent).as_posix()
                openers.add((module, enclosing.get(id(node), "<module>")))

    assert openers == {("guildbotics/drivers/command_runner.py", "run")}


@pytest.mark.asyncio
async def test_a_command_inside_another_is_held_to_what_the_outer_declared():
    """A command and its subcommands are one isolation: the same declaration
    shares it, and another cannot be given the one it declared."""
    from guildbotics.commands.errors import CommandError

    read_only = CommandAccess(read_only=True, inspects=frozenset({"config"}))
    async with environment.command_environment(read_only):
        outer = environment._COMMAND.get()
        async with environment.command_environment(read_only):
            assert environment._COMMAND.get() is outer
            assert environment.current_command_access() == read_only
        with pytest.raises(CommandError):
            async with environment.command_environment(CommandAccess()):
                pass

    assert environment.current_command_access() == CommandAccess()


@pytest.mark.asyncio
async def test_a_tool_not_logged_in_is_refused_only_when_a_turn_of_it_comes(
    tmp_path, monkeypatch
):
    """The command's other turns run; the microVM stays for them."""
    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
    )

    _device(monkeypatch, tmp_path, "claude")
    tools = frozenset({"claude", "codex"})

    async with environment.command_environment(CommandAccess()):
        turn = await environment.start_turn_environment(
            _turn(tmp_path, tools=tools), "claude"
        )
        await turn.close()
        (booted,) = _Booted.booted
        assert any(
            mount.guest.startswith(_state_root("codex")) for mount in booted.spec.mounts
        )
        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(
                _turn(tmp_path, "codex", tools=tools), "codex"
            )
        assert refused.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
        assert not booted.closed
        again = await environment.start_turn_environment(
            _turn(tmp_path, tools=tools), "claude"
        )
        await again.close()

    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("elsewhere", "outside_mounts"),
        ("inside a denied corner", "outside_mounts"),
        ("contract", "not_started_for"),
        ("tool", "not_started_for"),
    ],
)
async def test_a_turn_the_running_microvm_was_not_started_for_is_refused(
    tmp_path, monkeypatch, change, message
):
    """A running microVM is not reshaped: a turn it cannot hold as it is
    does not run, and the command's next turn still does."""
    from guildbotics.intelligences.agent_environment.contract import (
        AccessContract,
        DeniedPath,
        NetworkPolicy,
        ResolvedAccess,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
    )
    from guildbotics.utils.i18n_tool import t

    _device(monkeypatch, tmp_path, "claude", "codex")
    repository = tmp_path / "repository"
    denied = repository / "private"
    denied.mkdir(parents=True)
    contract = AccessContract(
        access=ResolvedAccess(denied=(DeniedPath(path=denied, builtin=False),))
    )
    turn = {
        "elsewhere": _turn(tmp_path, cwd=tmp_path / "elsewhere", contract=contract),
        "inside a denied corner": _turn(tmp_path, cwd=denied, contract=contract),
        "contract": _turn(
            tmp_path,
            contract=AccessContract(
                network=NetworkPolicy(mode="unrestricted"), access=contract.access
            ),
        ),
        "tool": _turn(tmp_path, "codex", contract=contract),
    }[change]

    async with environment.command_environment(CommandAccess()):
        first = await environment.start_turn_environment(
            _turn(tmp_path, contract=contract), "claude"
        )
        await first.close()
        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(
                turn, turn.conversation_key.adapter
            )
        again = await environment.start_turn_environment(
            _turn(tmp_path, contract=contract), "claude"
        )
        await again.close()

    assert refused.value.category is AgentRuntimeErrorCategory.CONFIGURATION
    key = f"intelligences.agent_environment.runtime.{message}"
    tool = environment.cli_agent_info(turn.conversation_key.adapter)
    assert str(refused.value) == t(key, path=turn.cwd, tool=tool.label)
    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
async def test_the_next_turn_waits_for_the_one_holding_the_microvm(
    tmp_path, monkeypatch
):
    """The member broker serves one turn at a time."""
    _device(monkeypatch, tmp_path, "claude")

    async with environment.command_environment(CommandAccess()):
        first = await environment.start_turn_environment(_turn(tmp_path), "claude")
        second = asyncio.create_task(
            environment.start_turn_environment(_turn(tmp_path), "claude")
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done()
        await first.close()
        await (await second).close()


@pytest.mark.asyncio
async def test_each_turn_is_lent_its_login_afresh(tmp_path, monkeypatch):
    """A login one turn could not use is not the next turn's: it is lent
    again, with a stand-in of its own that the previous one no longer opens."""
    from functools import reduce

    import httpx

    _device(monkeypatch, tmp_path, "claude")
    tool = environment.cli_agent_info("claude")
    broker = tool.provision.credential_broker
    auth = f"{_state_root('claude')}/{tool.provision.auth}"

    def stand_in(booted: _Booted) -> str:
        login = json.loads(booted.files[auth])
        return reduce(lambda at, key: at[key], broker.access_token, login)

    async def status(turn, token: str) -> int:
        (base_url,) = {turn.spec.env[variable] for variable in broker.base_url_env}
        port = base_url.rsplit(":", 1)[1].split("/", 1)[0]
        async with httpx.AsyncClient() as guest:
            answer = await guest.post(
                f"http://127.0.0.1:{port}/v1/oauth/token",
                headers={"authorization": f"Bearer {token}"},
            )
        return answer.status_code

    async with environment.command_environment(CommandAccess()):
        first_context = _turn(tmp_path)
        first = await environment.start_turn_environment(first_context, "claude")
        (booted,) = _Booted.booted
        first_stand_in = stand_in(booted)
        lent = first_context.login.refusal.__self__
        lent._failure = RuntimeError("refused in the first turn")
        await first.close()
        # Between turns the gateway takes no stand-in at all.
        between = await status(first, first_stand_in)
        second_context = _turn(tmp_path)
        second = await environment.start_turn_environment(second_context, "claude")
        second_stand_in = stand_in(booted)
        previous = await status(second, first_stand_in)
        current = await status(second, second_stand_in)
        await second.close()

    assert first_context.login.refusal() == "refused in the first turn"
    assert second_context.login.refusal() == ""
    assert first_stand_in != second_stand_in
    # 403: the stand-in is taken, the route is not forwarded.
    assert (between, previous, current) == (401, 401, 403)


@pytest.mark.asyncio
async def test_a_member_broker_that_does_not_start_is_a_process_error(
    tmp_path, monkeypatch
):
    """The broker's port is opened when the microVM boots, so nothing boots
    without it."""
    async with environment.command_environment(CommandAccess()):
        from guildbotics.intelligences.agent_runtime.member_broker import (
            MemberCapabilityBroker,
        )
        from guildbotics.intelligences.agent_runtime.models import (
            AgentRuntimeError,
            AgentRuntimeErrorCategory,
        )

        async def fail_to_start(_broker: MemberCapabilityBroker) -> None:
            raise OSError("bind failed")

        monkeypatch.setattr(MemberCapabilityBroker, "_start", fail_to_start)
        _device(monkeypatch, tmp_path, "claude")

        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(_turn(tmp_path), "claude")

        assert refused.value.category is AgentRuntimeErrorCategory.PROCESS
        assert _Booted.booted == []


@pytest.mark.asyncio
async def test_a_relayed_host_is_relayed_once_for_the_whole_command(
    tmp_path, monkeypatch
):
    """The relay is started by its tool's first turn and runs as long as the
    microVM; the next turn is relayed by it, and it ends with the microVM."""
    _device(monkeypatch, tmp_path, "antigravity")
    broker = environment.cli_agent_info("antigravity").provision.credential_broker
    assert broker.relayed_hosts

    async with environment.command_environment(CommandAccess()):
        for _ in range(2):
            turn = await environment.start_turn_environment(
                _turn(tmp_path, "antigravity"), "antigravity"
            )
            await turn.close()
        (booted,) = _Booted.booted
        ((_, relay),) = booted.relays
        hosts = booted.files["/etc/hosts"].decode()
        assert not relay.killed

    assert relay.killed
    for host in broker.relayed_hosts:
        assert hosts.count(f"127.0.0.2 {host}") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("inside", "admitted"), [(False, True), (True, False)])
async def test_a_contract_change_refuses_a_turn_only_if_the_microvm_would_show_it(
    tmp_path, monkeypatch, inside, admitted
):
    """A closed directory can appear while a command runs (a credential
    directory made on the host). Outside everything mounted it changes
    nothing the microVM holds; inside a mount it would have been covered."""
    from guildbotics.intelligences.agent_environment.contract import (
        AccessContract,
        DeniedPath,
        ResolvedAccess,
    )
    from guildbotics.intelligences.agent_runtime.models import AgentRuntimeError

    _device(monkeypatch, tmp_path, "claude")
    repository = tmp_path / "repository"
    closed = (repository if inside else tmp_path) / ".secrets"
    closed.mkdir(parents=True)
    later = AccessContract(
        access=ResolvedAccess(denied=(DeniedPath(path=closed, builtin=True),))
    )

    async with environment.command_environment(CommandAccess()):
        first = await environment.start_turn_environment(
            _turn(tmp_path, contract=AccessContract()), "claude"
        )
        await first.close()
        if admitted:
            second = await environment.start_turn_environment(
                _turn(tmp_path, contract=later), "claude"
            )
            await second.close()
        else:
            with pytest.raises(AgentRuntimeError):
                await environment.start_turn_environment(
                    _turn(tmp_path, contract=later), "claude"
                )

    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
async def test_a_boot_that_fails_leaves_no_gateway_running(tmp_path, monkeypatch):
    """The command may try another turn after a failed boot: that turn boots
    afresh, and no listener of the failed boot is left that no one can stop."""
    from guildbotics.intelligences.agent_environment.auth_gateway import (
        CredentialGateway,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
    )

    _device(monkeypatch, tmp_path, "claude", "codex")
    started: list[CredentialGateway] = []

    class Recorded(CredentialGateway):
        async def start(self) -> None:
            await super().start()
            started.append(self)

    monkeypatch.setattr(environment, "CredentialGateway", Recorded)
    booting = environment._start
    failures = [
        AgentRuntimeError(
            AgentRuntimeErrorCategory.PROCESS, "the microVM did not start"
        )
    ]

    async def start(spec, at, *, before_stop):
        if failures:
            raise failures.pop()
        return await booting(spec, at, before_stop=before_stop)

    monkeypatch.setattr(environment, "_start", start)
    tools = frozenset({"claude", "codex"})

    async with environment.command_environment(CommandAccess()):
        with pytest.raises(AgentRuntimeError):
            await environment.start_turn_environment(
                _turn(tmp_path, tools=tools), "claude"
            )
        failed = list(started)
        assert len(failed) == 2
        assert all(gateway._server is None for gateway in failed)
        turn = await environment.start_turn_environment(
            _turn(tmp_path, tools=tools), "claude"
        )
        await turn.close()
        assert len(started) == 4

    assert all(gateway._server is None for gateway in started)
