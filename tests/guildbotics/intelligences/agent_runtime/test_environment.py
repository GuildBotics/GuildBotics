from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

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


async def _claude_turn_spec(tmp_path, monkeypatch, context):
    """The spec a Claude Code turn run in ``context`` boots with."""
    from guildbotics.intelligences.agent_environment import provider_state

    tool = environment.cli_agent_info("claude")
    where = environment.LoginEnvironment(tmp_path / "snapshot", 1024, 1, ("1.1.1.1",))
    monkeypatch.setattr(environment, "_ready", lambda _: (tool, where))
    sealed = {tool.provision.auth: json.dumps(_LOGINS["claude"]).encode()}
    monkeypatch.setattr(provider_state, "_unsealed_login", lambda _: sealed)

    class Booted:
        def __init__(self, spec, before_stop):
            self.spec = spec
            self.before_stop = before_stop

        async def write_file(self, path, data):
            pass

    async def start(spec, at, *, before_stop):
        return Booted(spec, before_stop)

    monkeypatch.setattr(environment, "_start", start)
    booted = await environment.start_turn_environment(
        context, "claude", host_ports=(1234,), env={}
    )
    await booted.before_stop(booted)
    return booted.spec


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
        EnvironmentMount(guest_path(get_template_path()), get_template_path(), True),
    }
    if inspects:
        assert expected <= mounts
    else:
        assert not expected & mounts


def test_a_directory_not_there_yet_is_neither_mounted_nor_named(tmp_path):
    """Before the first run there is no run directory: the turn is not told to
    look in a place its environment does not have."""
    (tmp_path / ".guildbotics" / "config").mkdir(parents=True)

    directories = environment.inspected_directories({"diagnostics", "config"}, tmp_path)

    assert "diagnostics" not in directories
    assert directories["config"] == tmp_path / ".guildbotics" / "config"


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
    where = environment.LoginEnvironment(tmp_path / "snapshot", 1024, 1, ("1.1.1.1",))
    monkeypatch.setattr(environment, "_ready", lambda _: (tool, where))

    # The lending as it is, over a login that is not read from a vault.
    sealed = {tool.provision.auth: json.dumps(_LOGINS[tool_name]).encode()}
    monkeypatch.setattr(provider_state, "_unsealed_login", lambda selected: sealed)

    class Relay:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"ready\n")
            self.killed = False

        async def kill(self) -> None:
            self.killed = True

    class Booted:
        def __init__(self, spec, before_stop):
            self.spec = spec
            self.before_stop = before_stop
            self.files: dict[str, bytes] = {
                "/etc/ssl/certs/ca-certificates.crt": b"SYSTEM-CAS",
                "/etc/hosts": b"127.0.0.1 localhost",
            }
            self.relays: list[tuple[tuple[str, ...], Relay]] = []

        async def write_file(self, path, data):
            self.files[path] = data

        async def read_file(self, path):
            return self.files.get(path)

        async def run(self, *command, limit):
            relay = Relay()
            self.relays.append((command, relay))
            return relay

        async def close(self):
            await self.before_stop(self)

    async def start(spec, at, *, before_stop):
        assert at is where
        return Booted(spec, before_stop)

    monkeypatch.setattr(environment, "_start", start)
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path / "repository",
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", tool_name, "manual", "turn"),
        contract=AccessContract(),
    )

    booted = await environment.start_turn_environment(
        context, tool_name, host_ports=(1234,), env={"IS_SANDBOX": "1"}
    )

    spec = booted.spec
    scheme = "https" if broker.tls else "http"
    (base_url,) = {spec.env[variable] for variable in broker.base_url_env}
    port = int(base_url.removesuffix(broker.base_url_path).rsplit(":", 1)[1])
    assert base_url == f"{scheme}://{GUEST_HOST_ALIAS}:{port}{broker.base_url_path}"
    assert spec.network.host_ports == (1234, port)
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
            lambda at, key: at[key], broker.access_token, json.loads(booted.files[auth])
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

        await booted.close()
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
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.auth_gateway import (
        CredentialUnavailableError,
    )
    from guildbotics.intelligences.agent_environment.contract import AccessContract
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
        ConversationKey,
    )

    tool = environment.cli_agent_info("claude")
    where = environment.LoginEnvironment(tmp_path / "snapshot", 1024, 1, ("1.1.1.1",))
    monkeypatch.setattr(environment, "_ready", lambda _: (tool, where))
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

    class Booted:
        def __init__(self, spec, before_stop):
            self.spec = spec
            self.before_stop = before_stop

        async def write_file(self, path, data):
            pass

    async def start(spec, at, *, before_stop):
        happened.append("start")
        return Booted(spec, before_stop)

    monkeypatch.setattr(environment, "_start", start)
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path / "repository",
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "claude", "manual", "turn"),
        contract=AccessContract(),
    )

    if refreshes:
        booted = await environment.start_turn_environment(
            context, "claude", host_ports=(), env={}
        )
        await booted.before_stop(booted)
        assert happened == ["refresh", "start"]
    else:
        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(
                context, "claude", host_ports=(), env={}
            )
        assert refused.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
        assert str(refused.value) == "log in again"
        assert happened == ["refresh"]


@pytest.mark.asyncio
async def test_a_login_the_gateway_could_not_use_is_told_to_the_turn(
    tmp_path, monkeypatch
):
    """What the tool makes of a refused login is its own affair; the turn
    learns it from the login it lent."""
    import httpx

    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.auth_gateway import (
        CredentialGateway,
    )
    from guildbotics.intelligences.agent_environment.contract import AccessContract
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )
    from guildbotics.utils.i18n_tool import t

    tool = environment.cli_agent_info("copilot")
    where = environment.LoginEnvironment(tmp_path / "snapshot", 1024, 1, ("1.1.1.1",))
    monkeypatch.setattr(environment, "_ready", lambda _: (tool, where))
    sealed = {tool.provision.auth: json.dumps(_LOGINS["copilot"]).encode()}
    monkeypatch.setattr(provider_state, "_unsealed_login", lambda _: sealed)
    gateways: list[CredentialGateway] = []

    class Revoked(CredentialGateway):
        def __init__(self, *args, **kwargs):
            refuse = httpx.MockTransport(lambda _: httpx.Response(401))
            super().__init__(*args, transport=refuse, **kwargs)
            gateways.append(self)

    monkeypatch.setattr(environment, "CredentialGateway", Revoked)

    class Booted:
        def __init__(self, spec, before_stop):
            self.spec = spec
            self.before_stop = before_stop

        async def write_file(self, path, data):
            pass

    async def start(spec, at, *, before_stop):
        return Booted(spec, before_stop)

    monkeypatch.setattr(environment, "_start", start)
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path / "repository",
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "copilot", "manual", "turn"),
        contract=AccessContract(),
    )
    booted = await environment.start_turn_environment(
        context, "copilot", host_ports=(), env={}
    )
    try:
        assert context.login.refusal() == ""
        (gateway,) = gateways
        async with httpx.AsyncClient() as guest:
            await guest.get(
                f"http://127.0.0.1:{gateway.port}/models",
                headers={"authorization": f"Bearer {gateway.stand_in}"},
            )
    finally:
        await booted.before_stop(booted)

    assert context.login.refusal() == t(
        "intelligences.agent_environment.tool.login_refused",
        tool=tool.label,
        command=provider_state.login_command("copilot"),
    )
