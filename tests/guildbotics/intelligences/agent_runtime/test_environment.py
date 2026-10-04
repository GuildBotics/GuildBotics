from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    ResolvedAccess,
    ResolvedGrant,
)
from guildbotics.intelligences.agent_runtime import environment
from guildbotics.intelligences.agent_runtime.host_client import (
    MEMBER_BROKER_TOKEN_ENV,
)
from guildbotics.runtime.member_invocation import GuestProcessError, GuestResult
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root
from tests.guildbotics.intelligences.agent_runtime.contract_doubles import (
    command_at,
    settle_contract,
)


@pytest.fixture(autouse=True)
def _default_contract(monkeypatch) -> None:
    """Every command here reads the default contract unless a test says
    otherwise, whatever the settings of the machine running the tests."""
    settle_contract(monkeypatch, AccessContract())


def _command(
    *tools: str, access: CommandAccess = CommandAccess(), cwd: Path | None = None
):
    """A command of aiko, a member configured with ``tools`` (Claude Code by
    default), declaring ``access`` and working in ``cwd`` (``repository`` in
    the test's workspace by default)."""
    cwd = cwd or get_workspace_root() / "repository"
    cwd.mkdir(parents=True, exist_ok=True)
    return command_at(cwd, tools or {"claude"}, access)


def test_no_adapter_starts_a_process_but_through_its_turn() -> None:
    """A provider runs as its turn's process in the command's microVM,
    started through the turn: the turn is the one place in the agent runtime
    that starts a process, and the host starts none of its own."""
    runtime_dir = Path(environment.__file__).parent
    starting = [
        path.name
        for path in runtime_dir.glob("*.py")
        if re.search(
            r"create_subprocess_|\bsubprocess\.|import subprocess|\bPopen\b"
            r"|\bchild_process\b",
            path.read_text(encoding="utf-8"),
        )
    ]

    assert starting == ["turn.py"]


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


class _Program:
    """A process GuildBotics runs in a microVM: it writes back what it read
    once its input closes, or, ``forever``, never ends."""

    def __init__(self, command, cwd, env, *, forever: bool) -> None:
        self.command, self.cwd, self.env = command, cwd, env
        self.forever = forever
        self.read = bytearray()
        self.stdin = self
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._exited = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.read += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        if not self.forever:
            self.stdout.feed_data(bytes(self.read))
            self._end(0)

    def _end(self, returncode: int) -> None:
        if self.returncode is None:
            self.returncode = returncode
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def kill(self) -> None:
        self.killed = True
        self._end(-1)


class _Booted:
    """A microVM as the runtime boots it: its files, what runs in it, and
    whether it was stopped."""

    booted: list[_Booted] = []
    #: Whether what GuildBotics runs in it never ends.
    forever = False

    def __init__(self, spec, before_stop) -> None:
        self.spec = spec
        self.before_stop = before_stop
        self.files: dict[str, bytes] = {
            "/etc/ssl/certs/ca-certificates.crt": b"SYSTEM-CAS",
            "/etc/hosts": b"127.0.0.1 localhost",
        }
        self.relays: list[tuple[tuple[str, ...], _Relay]] = []
        self.programs: list[_Program] = []
        self.closed = False
        _Booted.booted.append(self)

    async def write_file(self, path, data):
        self.files[path] = data

    async def read_file(self, path):
        return self.files.get(path)

    async def run(self, *command, limit, cwd=None, env=None, **_):
        if command[0] != "node":
            program = _Program(command, cwd, env, forever=_Booted.forever)
            self.programs.append(program)
            return program
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
    monkeypatch.setattr(environment, "_device", lambda: where)

    def unsealed(tool):
        if tool.name not in logins:
            raise CredentialVaultError("missing")
        return {tool.provision.auth: json.dumps(_LOGINS[tool.name]).encode()}

    monkeypatch.setattr(provider_state, "_unsealed_login", unsealed)
    _Booted.booted = []
    _Booted.forever = False

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
            "conversation_key": ConversationKey("aiko", tool_name, "manual", "turn"),
            **overrides,
        }
    )


async def _claude_turn_spec(tmp_path, monkeypatch, context, access=CommandAccess()):
    """The spec a Claude Code turn run in ``context`` of a command declaring
    ``access``, and working where the turn does, boots with."""
    _device(monkeypatch, tmp_path, "claude")
    async with _command(access=access, cwd=context.cwd):
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
    from guildbotics.intelligences.agent_environment.contract import NetworkPolicy
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationKey,
    )

    settle_contract(
        monkeypatch, AccessContract(network=NetworkPolicy(mode="unrestricted"))
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
        conversation_key=ConversationKey("aiko", "claude", "troubleshooting", "c1"),
    )

    spec = await _claude_turn_spec(
        tmp_path, monkeypatch, context, CommandAccess(read_only=True)
    )

    writable = {
        mount.host
        for mount in spec.mounts
        if mount.host is not None and not mount.readonly
    }
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
    """The configuration is mounted read-only for every command, which reads
    it; the recorded runs only for a command that declares its turns inspect
    them, and a turn is told of either only then."""
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
        conversation_key=ConversationKey("aiko", "claude", "troubleshooting", "c1"),
    )
    spec = await _claude_turn_spec(
        tmp_path, monkeypatch, context, CommandAccess(inspects=inspects)
    )

    mounts = set(spec.mounts)
    runs = EnvironmentMount(guest_path(run), run, True)
    assert EnvironmentMount(guest_path(state / "config"), state / "config", True) in (
        mounts
    )
    if inspects:
        assert runs in mounts
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
        assert runs not in mounts
        assert environment.inspected_directories(inspects, tmp_path) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_every_turn_has_the_running_code_read_only_apart_from_the_users(
    tmp_path, monkeypatch, read_only
):
    """GuildBotics' own code -- the package this process runs -- is in every
    microVM at a place of its own, read-only, so a turn working in the
    checkout it runs from still writes the package there."""
    import guildbotics
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
        conversation_key=ConversationKey("aiko", "claude", "manual", "r1"),
    )
    spec = await _claude_turn_spec(
        tmp_path, monkeypatch, context, CommandAccess(read_only=read_only)
    )

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
            guest_path(checkout), None if read_only else checkout, False, user=True
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


def test_linked_package_uses_one_canonical_root_for_code_and_templates(
    tmp_path, symlinks
):
    import subprocess
    import sys
    from guildbotics.utils.fileio import PACKAGE_ROOT

    linked = tmp_path / "launcher"
    linked.symlink_to(PACKAGE_ROOT.parent, target_is_directory=True)
    code = "import sys; sys.path.insert(0, sys.argv[1]); import guildbotics; from guildbotics.utils.fileio import PACKAGE_ROOT, get_template_path; from guildbotics.intelligences.agent_runtime.environment import CODE_MOUNT, code_path, command_path; assert sys.argv[1] in guildbotics.__file__; assert CODE_MOUNT.host == PACKAGE_ROOT; assert code_path(get_template_path()).startswith(CODE_MOUNT.guest); assert command_path(get_template_path() / 'ask.en.md').startswith(CODE_MOUNT.guest)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(linked)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
    context = _turn(tmp_path, tool_name)

    running = AsyncExitStack()
    await running.enter_async_context(_command(tool_name))
    turn = await environment.start_turn_environment(context, tool_name)
    (booted,) = _Booted.booted

    spec = turn.spec
    scheme = "https" if broker.tls else "http"
    (base_url,) = {spec.env[variable] for variable in broker.base_url_env}
    port = int(base_url.removesuffix(broker.base_url_path).rsplit(":", 1)[1])
    assert base_url == f"{scheme}://{GUEST_HOST_ALIAS}:{port}{broker.base_url_path}"
    assert spec.network.host_ports == (turn.broker.endpoint.port, port)
    assert set(spec.network.domains) == set(broker.turn_domains)
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
    from guildbotics.intelligences.agent_environment import provider_state
    from guildbotics.intelligences.agent_environment.auth_gateway import (
        CredentialUnavailableError,
    )
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
    context = _turn(tmp_path)

    async with _command():
        if refreshes:
            turn = await environment.start_turn_environment(context, "claude")
            await turn.close()
        else:
            with pytest.raises(AgentRuntimeError) as refused:
                await environment.start_turn_environment(context, "claude")
            assert refused.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
            assert str(refused.value) == "log in again"
        # The command's microVM runs from its start; the turn is refreshed
        # before it is lent anything.
        assert happened == ["start", "refresh"]


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
    async with _command("copilot"):
        context = _turn(tmp_path, "copilot")
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
    tools = ("claude", "codex")
    repository = tmp_path / "repository"

    async with _command(*tools):
        first = await environment.start_turn_environment(
            _turn(tmp_path, "claude"), "claude"
        )
        await first.close()
        second = await environment.start_turn_environment(
            _turn(tmp_path, "codex", cwd=repository / "package"),
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
@pytest.mark.parametrize("where", ["exchange", "workspace root"])
@pytest.mark.parametrize("read_only", [False, True])
async def test_the_microvm_works_where_the_command_does_and_its_turns_in_the_clone(
    tmp_path, monkeypatch, where, read_only
):
    """The microVM is shaped from the command, not from its first turn: a
    workflow the host starts works in the exchange directory while its turn
    works in the member's clone, which the host makes before the boot and
    mounts read-write -- inside the cover over the workspace's own state
    when the command works in the workspace root. A read-only command
    changes nothing, so it has no clone to work in, nor makes one."""
    from guildbotics.intelligences.agent_environment.contract import (
        DeniedPath,
        ResolvedAccess,
    )
    from guildbotics.intelligences.agent_environment.spec import (
        EnvironmentMount,
        guest_path,
    )

    state = tmp_path / ".guildbotics"
    settle_contract(
        monkeypatch,
        AccessContract(
            access=ResolvedAccess(denied=(DeniedPath(path=state, builtin=True),))
        ),
    )
    _device(monkeypatch, tmp_path, "claude")
    cwd = {"exchange": tmp_path / "exchange", "workspace root": tmp_path}[where]
    cwd.mkdir(exist_ok=True)
    clone = get_member_clone_path("aiko")
    in_clone = _turn(tmp_path, cwd=clone / "src")
    turn_context = _turn(tmp_path, cwd=cwd) if read_only else in_clone
    if where == "workspace root" and not read_only:
        from guildbotics.commands.errors import CommandError

        with pytest.raises(CommandError):
            async with _command(cwd=cwd):
                pass
        assert not _Booted.booted
        return
    async with _command(cwd=cwd, access=CommandAccess(read_only=read_only)):
        turn = await environment.start_turn_environment(turn_context, "claude")
        await turn.close()
        (booted,) = _Booted.booted

    assert turn.spec.cwd == guest_path(turn_context.cwd)
    assert booted.spec.cwd == guest_path(cwd)
    assert clone.is_dir() != read_only
    mounted = EnvironmentMount(guest_path(clone), clone, readonly=False)
    assert (mounted in booted.spec.mounts) != read_only
    guests = [mount.guest for mount in booted.spec.mounts]
    assert (guest_path(state) in guests) == (
        where == "workspace root" and not read_only
    )


@pytest.mark.asyncio
async def test_the_workspace_settings_are_read_once_when_the_command_starts(
    tmp_path, monkeypatch
):
    """Every turn of a command, whatever tool runs it, is held to the network
    the workspace declares; the settings are read when the command starts,
    not by each turn, since nothing a turn read could reshape the microVM."""
    from guildbotics.intelligences.agent_environment.contract import NetworkPolicy
    from guildbotics.intelligences.agent_environment.toolchain import (
        DnsSettings,
        ToolchainDeclaration,
    )

    _device(monkeypatch, tmp_path, "claude", "codex")
    network = NetworkPolicy(
        mode="allowlist",
        allowed_domains=["registry.npmjs.org"],
        allow_local_network=False,
    )
    reads: list[str] = []

    def declared() -> ToolchainDeclaration:
        reads.append("toolchain")
        return ToolchainDeclaration(
            network=network, dns=DnsSettings(nameservers=["1.1.1.1"])
        )

    monkeypatch.setattr(environment, "load_toolchain", declared)

    async with _command("claude", "codex"):
        assert reads == ["toolchain"]
        for tool_name in ("claude", "codex"):
            turn = await environment.start_turn_environment(
                _turn(tmp_path, tool_name), tool_name
            )
            await turn.close()

    (booted,) = _Booted.booted
    assert "registry.npmjs.org" in booted.spec.network.domains
    assert reads == ["toolchain"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "cancellation"])
async def test_a_command_that_does_not_end_well_still_discards_its_microvm(
    tmp_path, monkeypatch, ending
):
    _device(monkeypatch, tmp_path, "claude")
    started = asyncio.Event()

    async def command() -> None:
        async with _command():
            await environment.start_turn_environment(_turn(tmp_path), "claude")
            started.set()
            if ending == "failure":
                raise RuntimeError("the command failed")
            await asyncio.Event().wait()

    task = asyncio.create_task(command())
    waiting = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    # A turn that does not start fails the test here, rather than leaving it
    # waiting for a start that never comes.
    assert started.is_set(), task.exception()
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


def _call_sites(
    matches: Callable[[ast.Call], bool], *, awaited: bool = False
) -> set[tuple[str, str]]:
    """The package module and enclosing function of every call ``matches``;
    only of the awaited ones when ``awaited``."""
    import guildbotics

    package = Path(guildbotics.__file__).parent
    sites: set[tuple[str, str]] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing = {
            id(node): function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        calls = (
            [node.value for node in ast.walk(tree) if isinstance(node, ast.Await)]
            if awaited
            else ast.walk(tree)
        )
        for node in calls:
            if isinstance(node, ast.Call) and matches(node):
                module = path.relative_to(package.parent).as_posix()
                sites.add((module, enclosing.get(id(node), "<module>")))
    return sites


def test_only_where_a_command_runs_is_a_command_environment_opened() -> None:
    """No caller runs an AI CLI turn outside a command.

    A turn refuses to start outside a command's environment, so the places
    that open one are the population of places a turn can run from. Each must
    be where a command runs; a caller that opened one of its own would run
    its turns outside any command.
    """
    openers = _call_sites(
        lambda call: (
            (getattr(call.func, "id", None) or getattr(call.func, "attr", None))
            == "command_environment"
        )
    )

    assert openers == {("guildbotics/drivers/command_runner.py", "run_in_environment")}


def test_every_bind_and_microvm_entry_uses_the_checked_mount_boundary() -> None:
    """New mounts or VM entries must explicitly join the common safety check."""
    assert _call_sites(
        lambda call: (
            getattr(call.func, "attr", None) == "bind"
            and getattr(getattr(call.func, "value", None), "id", None) == "Volume"
        )
    ) == {("guildbotics/intelligences/agent_environment/runtime.py", "_volumes")}
    assert _call_sites(
        lambda call: (
            (
                getattr(call.func, "attr", None) == "create"
                and getattr(getattr(call.func, "value", None), "id", None) == "Sandbox"
            )
            or (
                getattr(call.func, "attr", None) == "create"
                and getattr(getattr(call.func, "value", None), "attr", None)
                == "Sandbox"
            )
        )
    ) == {
        ("guildbotics/intelligences/agent_environment/runtime.py", "start"),
        ("guildbotics/intelligences/agent_environment/runtime.py", "build_snapshot"),
    }
    assert _call_sites(
        lambda call: getattr(call.func, "id", None) == "EnvironmentMount"
    ) == {
        ("guildbotics/intelligences/agent_environment/spec.py", "_mounts"),
        ("guildbotics/intelligences/agent_environment/provider_state.py", "bind_state"),
        (
            "guildbotics/intelligences/agent_environment/provider_state.py",
            "_state_root_spec",
        ),
        ("guildbotics/intelligences/agent_runtime/environment.py", "<module>"),
        ("guildbotics/intelligences/agent_runtime/environment.py", "_inspected_mounts"),
    }


def test_every_command_runs_in_an_environment() -> None:
    """The host runs every command in the environment booted for it.

    The execution machinery runs the commands it holds with
    ``await ....run()``; outside it, only the environment's entry does so,
    and the entry is started only where the host runs a command in its
    environment.
    """
    starters = {
        site
        for site in _call_sites(
            lambda call: (
                getattr(call.func, "attr", None) == "run"
                and not call.args
                and not call.keywords
            ),
            awaited=True,
        )
        if not site[0].startswith("guildbotics/commands/")
    }
    entries = _call_sites(
        lambda call: (
            getattr(call.func, "attr", None) == "execute" and len(call.args) == 1
        ),
        awaited=True,
    )

    assert starters == {("guildbotics/runtime/command_entry.py", "run")}
    assert entries == {("guildbotics/drivers/command_runner.py", "run_in_environment")}


def test_every_host_entry_names_the_working_directory_of_its_command() -> None:
    """A command works where its host entry says, never where the process
    happens to be: the process's working directory differs between the
    Desktop and ``guildbotics start``. What the host starts on its own works
    in the exchange directory; what a person starts works where they said.

    The population is every call that makes a command to run --
    ``CommandRunner`` and the host entries that make one -- and each is
    listed with what it passes as the working directory, so a new entry has
    to say where its command works.
    """
    import guildbotics

    makers = {
        "guildbotics.commands.runner": {"CommandRunner": 3},
        "guildbotics.drivers.command_runner": {
            "_prepared": 3,
            "prepare_command": 4,
            "run_command": 4,
        },
    }
    package = Path(guildbotics.__file__).parent
    sites: dict[tuple[str, str], str] = {}
    for path in sorted(package.rglob("*.py")):
        module = path.relative_to(package.parent).with_suffix("").as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        known = dict(makers.get(module.replace("/", "."), {}))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in makers:
                known.update(
                    (alias.asname or alias.name, makers[node.module][alias.name])
                    for alias in node.names
                    if alias.name in makers[node.module]
                )
        enclosing = {
            id(node): function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in known
            ):
                continue
            position = known[call.func.id]
            cwd = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "cwd"),
                call.args[position] if len(call.args) > position else None,
            )
            sites[(f"{module}.py", enclosing.get(id(call), "<module>"))] = (
                ast.unparse(cwd) if cwd is not None else "<none>"
            )

    assert sites == {
        ("guildbotics/drivers/command_runner.py", "run_command"): "cwd",
        (
            "guildbotics/drivers/command_runner.py",
            "prepare_host_command",
        ): "host_command_cwd()",
        ("guildbotics/drivers/command_runner.py", "prepare_command"): "cwd",
        ("guildbotics/runtime/command_entry.py", "run"): "Path(request.cwd)",
        (
            "guildbotics/app_api/runtime.py",
            "_execute_command",
        ): "execution.cwd()",
        ("guildbotics/app_api/diagnostics.py", "_run_cli_agent_check"): "Path(cwd)",
        ("guildbotics/runtime/local_command_executor.py", "run"): "cwd",
    }


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

    async with _command("claude", "codex"):
        turn = await environment.start_turn_environment(_turn(tmp_path), "claude")
        await turn.close()
        (booted,) = _Booted.booted
        assert any(
            mount.guest.startswith(_state_root("codex")) for mount in booted.spec.mounts
        )
        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(_turn(tmp_path, "codex"), "codex")
        assert refused.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
        assert not booted.closed
        again = await environment.start_turn_environment(_turn(tmp_path), "claude")
        await again.close()

    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("elsewhere", "outside_mounts"),
        ("inside a denied corner", "outside_mounts"),
        ("tool", "not_started_for"),
    ],
)
@pytest.mark.parametrize("first", [False, True], ids=["later turn", "first turn"])
async def test_a_turn_the_running_microvm_was_not_started_for_is_refused(
    tmp_path, monkeypatch, change, message, first
):
    """A running microVM is not reshaped: a turn it cannot hold as it is
    does not run, and the command's next turn still does. The command, not
    its first turn, shapes it, so a first turn is held to it the same way."""
    from guildbotics.intelligences.agent_environment.contract import (
        DeniedPath,
        ResolvedAccess,
    )
    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
    )
    from guildbotics.utils.i18n_tool import t

    _device(monkeypatch, tmp_path, "claude", "codex")
    repository = tmp_path / "repository"
    denied = tmp_path / "private"
    denied.mkdir(parents=True)
    settle_contract(
        monkeypatch,
        AccessContract(
            access=ResolvedAccess(denied=(DeniedPath(path=denied, builtin=False),))
        ),
    )
    turn = {
        "elsewhere": _turn(tmp_path, cwd=tmp_path / "elsewhere"),
        "inside a denied corner": _turn(tmp_path, cwd=denied),
        # Logged in, but not a tool the member was configured with when the
        # command started.
        "tool": _turn(tmp_path, "codex"),
    }[change]

    async with _command():
        if not first:
            earlier = await environment.start_turn_environment(
                _turn(tmp_path), "claude"
            )
            await earlier.close()
        with pytest.raises(AgentRuntimeError) as refused:
            await environment.start_turn_environment(
                turn, turn.conversation_key.adapter
            )
        again = await environment.start_turn_environment(_turn(tmp_path), "claude")
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

    async with _command():
        first = await environment.start_turn_environment(_turn(tmp_path), "claude")
        second = asyncio.create_task(
            environment.start_turn_environment(_turn(tmp_path), "claude")
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done()
        await first.close()
        await (await second).close()


async def _member_command(turn, *, seconds: float = 5.0, **options: Any):
    """Run a process in the turn's microVM the way a member command the
    broker runs does: from a worker thread, while the broker holds the
    command it is running."""
    guest = turn.broker._guest.until(time.monotonic() + seconds)
    async with turn.broker._command_lock:
        return await asyncio.to_thread(
            partial(
                guest.run,
                **{"cwd": "/work", "env": {}, "stdout_limit": 1 << 10, **options},
            )
        )


@pytest.mark.asyncio
async def test_a_member_command_runs_in_the_microvm_the_turn_holds(
    tmp_path, monkeypatch
):
    """In the middle of the turn that asked for it, holding neither the turn
    nor the broker, with the host's facts and nothing of the turn's."""
    _device(monkeypatch, tmp_path, "claude")

    async with _command():
        turn = await environment.start_turn_environment(_turn(tmp_path), "claude")
        source = tmp_path / "bundle"
        source.write_bytes(b"history")
        written = tmp_path / "written"
        streamed = await _member_command(
            turn, argv=["git", "fetch"], stdin=source, stdout=written
        )
        result = await _member_command(
            turn,
            argv=["git", "commit"],
            env={"GIT_AUTHOR_NAME": "Aiko"},
            stdin=b"message",
        )
        await turn.close()

    [booted] = _Booted.booted
    assert streamed == GuestResult(0, b"", b"")
    assert written.read_bytes() == b"history"
    assert result == GuestResult(0, b"message", b"")
    program = booted.programs[-1]
    assert (program.command, program.cwd) == (("git", "commit"), "/work")
    assert program.env == {**booted.spec.env, "GIT_AUTHOR_NAME": "Aiko"}
    assert MEMBER_BROKER_TOKEN_ENV not in program.env


@pytest.mark.asyncio
async def test_a_member_command_runs_nothing_it_would_have_to_wait_for_itself(
    tmp_path, monkeypatch
):
    """Not from the command's own loop, which the process has to run on, and
    not without the time the broker gives it."""
    _device(monkeypatch, tmp_path, "claude")

    async with _command():
        turn = await environment.start_turn_environment(_turn(tmp_path), "claude")
        guest = turn.broker._guest
        options = {"cwd": "/work", "env": {}, "stdout_limit": 1}
        with pytest.raises(GuestProcessError, match="another thread"):
            guest.until(time.monotonic() + 5).run(["git", "status"], **options)
        with pytest.raises(GuestProcessError, match="out of time"):
            await asyncio.to_thread(partial(guest.run, ["git", "status"], **options))
        await turn.close()

    assert _Booted.booted[0].programs == []


@pytest.mark.asyncio
async def test_a_member_command_the_broker_gave_up_on_leaves_nothing_running(
    tmp_path, monkeypatch
):
    """Its thread returns when the broker stops waiting, and what it ran in
    the microVM is ended; so is what writes more than it may."""
    _device(monkeypatch, tmp_path, "claude")

    async with _command():
        turn = await environment.start_turn_environment(_turn(tmp_path), "claude")
        with pytest.raises(GuestProcessError, match="more than 1024 bytes"):
            await _member_command(turn, argv=["git", "bundle"], stdin=b"x" * 2048)
        _Booted.forever = True
        started = time.monotonic()
        with pytest.raises(GuestProcessError, match="out of time"):
            await _member_command(turn, seconds=0.2, argv=["git", "fetch"])
        assert time.monotonic() - started < 2
        for _ in range(10):
            await asyncio.sleep(0)
        await turn.close()

    [booted] = _Booted.booted
    assert [program.killed for program in booted.programs] == [True, True]


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

    async with _command():
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
        async with _command():
            pass

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

    async with _command("antigravity"):
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
async def test_a_boot_that_fails_leaves_no_gateway_running(tmp_path, monkeypatch):
    """A command whose microVM does not start leaves no listener of it that
    no one can stop, and the next command boots afresh."""
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

    with pytest.raises(AgentRuntimeError):
        async with _command("claude", "codex"):
            pass
    failed = list(started)
    assert len(failed) == 2
    assert all(gateway._server is None for gateway in failed)
    async with _command("claude", "codex"):
        turn = await environment.start_turn_environment(_turn(tmp_path), "claude")
        await turn.close()
        assert len(started) == 4

    assert all(gateway._server is None for gateway in started)


class _Entry:
    """The command's entry as the microVM runs it: it reads its request, logs
    what ``log`` says, answers ``reply``, and ends with ``returncode``; or,
    ``forever``, never ends; or, ``lingers``, answers but leaves a process
    holding its output open."""

    def __init__(
        self,
        reply: bytes,
        log: bytes = b"",
        *,
        returncode=0,
        forever=False,
        lingers=False,
    ):
        self.reply, self.log = reply, log
        self.returncode_on_end, self.forever = returncode, forever
        self.lingers = lingers
        self.read = bytearray()
        self.stdin = self
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self.argv: tuple[str, ...] = ()
        self._ended = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.read += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        if self.forever:
            return
        self.stderr.feed_data(self.log)
        self.stdout.feed_data(self.reply)
        if not self.lingers:
            self._end(self.returncode_on_end)

    def _end(self, returncode: int) -> None:
        if self.returncode is None:
            self.returncode = returncode
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._ended.set()

    async def wait(self) -> int:
        await self._ended.wait()
        assert self.returncode is not None
        return self.returncode

    async def kill(self) -> None:
        self.killed = True
        self._end(-9)


async def _executed(tmp_path, monkeypatch, entry: _Entry):
    """Run a command in a microVM whose entry is ``entry``; how it ended."""
    from guildbotics.intelligences.agent_runtime.host_client import CommandRequest

    _device(monkeypatch, tmp_path, "claude")
    async with _command() as shared:
        (booted,) = _Booted.booted

        async def run(*argv, limit, **_):
            entry.argv = argv
            return entry

        booted.run = run
        request = CommandRequest(path="/c/x.md", name="x", args=[], cwd="/work")
        try:
            return await shared.execute(request), request
        finally:
            assert json.loads(entry.read) == request.model_dump(mode="json")


@pytest.mark.asyncio
async def test_the_command_runs_in_its_microvm_and_says_how_it_ended(
    tmp_path, monkeypatch, caplog
):
    """GuildBotics' own entry runs the request in the microVM with the
    snapshot's Python and the running code; what it logs is logged on the
    host at its own level, a line without one at the level before it."""
    from guildbotics.intelligences.agent_environment.snapshot import VENV
    from guildbotics.intelligences.agent_runtime.host_client import CommandReply

    answer = CommandReply(text_output="done")
    entry = _Entry(
        answer.model_dump_json().encode() + b"\n",
        b"INFO started\nWARNING careful\n  continued\n",
    )

    with caplog.at_level(logging.INFO, logger="guildbotics"):
        reply, _ = await _executed(tmp_path, monkeypatch, entry)

    assert reply == answer
    assert entry.argv == (
        f"{VENV}/bin/python",
        "-B",
        "-m",
        "guildbotics.runtime.command_entry",
    )
    logged = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.getMessage() in {"started", "careful", "  continued"}
    ]
    assert logged == [
        ("INFO", "started"),
        ("WARNING", "careful"),
        ("WARNING", "  continued"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "key"),
    [
        (lambda: _Entry(b"", b"Traceback ...\n", returncode=1), "no_reply"),
        (lambda: _Entry(b"not json", returncode=0), "no_reply"),
        (lambda: _Entry(b"x" * (10 * 1024 * 1024 + 1)), "reply_too_large"),
    ],
    ids=["crashed", "garbled", "too-large"],
)
async def test_an_entry_that_does_not_say_how_the_command_ended_fails_it(
    tmp_path, monkeypatch, entry, key
):
    from guildbotics.commands.errors import CommandError
    from guildbotics.utils.i18n_tool import t

    entry = entry()

    with pytest.raises(CommandError) as failed:
        await _executed(tmp_path, monkeypatch, entry)

    assert str(failed.value) == t(
        f"intelligences.agent_environment.runtime.{key}",
        code=entry.returncode,
    )


@pytest.mark.asyncio
async def test_a_process_the_command_left_running_does_not_hold_its_end(
    tmp_path, monkeypatch
):
    """The entry's reply is the line it writes last: a process the command
    left running holds the entry's log open, not the command, which ends
    with what the entry said (and the process with the microVM)."""
    from guildbotics.intelligences.agent_runtime.host_client import CommandReply

    monkeypatch.setattr(environment, "_LOG_DRAIN_SECONDS", 0.01)
    answer = CommandReply(text_output="done")
    entry = _Entry(answer.model_dump_json().encode() + b"\n", lingers=True)

    reply, _ = await asyncio.wait_for(_executed(tmp_path, monkeypatch, entry), 5)

    assert reply == answer


@pytest.mark.asyncio
async def test_a_command_stopped_while_it_runs_ends_its_entry(tmp_path, monkeypatch):
    """A stop cancels the host's wait, and nothing of the command is left
    running in the microVM, which goes with it."""
    entry = _Entry(b"", forever=True)
    running = asyncio.create_task(_executed(tmp_path, monkeypatch, entry))
    for _ in range(50):
        if entry.read:
            break
        await asyncio.sleep(0.01)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert entry.killed
    (booted,) = _Booted.booted
    assert booted.closed


@pytest.mark.asyncio
async def test_a_device_that_cannot_run_the_environment_refuses_the_command(
    tmp_path, monkeypatch
):
    """Every command runs in the environment, so a device that cannot run
    one refuses the command when it starts, in the words its status gives."""
    from types import SimpleNamespace

    from guildbotics.commands.errors import CommandError

    booted: list[object] = []
    monkeypatch.setattr(environment, "_start", lambda *args, **_: booted.append(args))
    monkeypatch.setattr(
        environment,
        "device_status",
        lambda: SimpleNamespace(snapshot=None, refusal="the snapshot is building"),
    )

    with pytest.raises(CommandError, match="the snapshot is building"):
        async with _command():
            pass

    assert booted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cwd", "grant"])
async def test_windows_temp_mount_is_refused_before_boot(
    tmp_path, monkeypatch, fake_platform, source
):
    from guildbotics.commands.errors import CommandError

    fake_platform(environment, "win32")
    temporary = tmp_path / "Local" / "Temp"
    mounted = temporary / "work"
    mounted.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(temporary.parent))
    monkeypatch.setenv("TEMP", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("TMP", str(tmp_path / "elsewhere"))
    if source == "grant":
        settle_contract(
            monkeypatch,
            AccessContract(
                access=ResolvedAccess(
                    paths=(ResolvedGrant(mounted, "read", str(mounted)),)
                )
            ),
        )
    _device(monkeypatch, tmp_path)

    with pytest.raises(CommandError) as failed:
        async with _command(
            cwd=mounted if source == "cwd" else tmp_path / "repository"
        ):
            pass

    assert str(mounted) in str(failed.value)
    assert str(temporary) in str(failed.value)
    assert "4096" not in str(failed.value)
    assert _Booted.booted == []


@pytest.mark.asyncio
async def test_windows_temp_workdir_is_allowed_when_it_is_not_host_bound(
    tmp_path, monkeypatch, fake_platform
):
    fake_platform(environment, "win32")
    temporary = tmp_path / "Local" / "Temp"
    monkeypatch.setenv("LOCALAPPDATA", str(temporary.parent))
    _device(monkeypatch, tmp_path)

    async with _command(access=CommandAccess(read_only=True), cwd=temporary / "work"):
        pass

    assert len(_Booted.booted) == 1


@pytest.mark.asyncio
async def test_windows_temp_workspace_config_is_refused_for_read_only_command(
    tmp_path, monkeypatch, fake_platform
):
    from guildbotics.commands.errors import CommandError
    from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

    fake_platform(environment, "win32")
    temporary = tmp_path / "Local" / "Temp"
    workspace = temporary / "workspace"
    config = workspace / ".guildbotics" / "config"
    config.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(temporary.parent))
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace))
    _device(monkeypatch, tmp_path)

    with pytest.raises(CommandError) as failed:
        async with _command(
            access=CommandAccess(read_only=True), cwd=tmp_path / "outside"
        ):
            pass

    assert str(config) in str(failed.value)
    assert _Booted.booted == []


@pytest.mark.asyncio
async def test_windows_temp_mount_is_not_rejected_on_other_platforms(
    tmp_path, monkeypatch, fake_platform
):
    fake_platform(environment, "linux")
    temporary = tmp_path / "Local" / "Temp"
    mounted = temporary / "work"
    monkeypatch.setenv("LOCALAPPDATA", str(temporary.parent))
    _device(monkeypatch, tmp_path)

    async with _command(cwd=mounted):
        pass

    assert len(_Booted.booted) == 1
