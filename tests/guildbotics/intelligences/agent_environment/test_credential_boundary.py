"""Where a login is, and where it never is, observed in real microVMs.

Skipped unless ``GUILDBOTICS_CONTRACT_PROBE=1``, on a device whose agent
environment is ready (point ``GUILDBOTICS_CONFIG_DIR`` at a workspace with a
built snapshot), like the connection contracts beside it. Every login is
synthetic, and every secret in it carries ``MARK``; the store and the sealed
login live under the test's own directory and the keychain is the suite's
in-memory one. Nothing is sent off the device: a turn's gateway answers from
an upstream that echoes what it was sent, and an environment that holds the
login reaches no domain.

What is looked for, and where:

- a turn: every file of the microVM, and the environment and command line of
  every process in it; what the gateway answers, from an upstream that
  echoes the token back; and, once the turn is over, the files it left on
  this device.
- where the login is held (a refresh, a probe): the microVM's writable layer
  and logs on this device's disk while it runs -- what a power cut would
  leave -- and that nothing of it is left once it stops, whether it ended,
  was cancelled, timed out, or its owner was killed.
- the snapshot every environment boots from.

Run it serially (``-p no:xdist``): each check tells its microVM from the
others by the sandboxes that appear while it runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from guildbotics.intelligences.agent_environment import (
    credential_vault,
    provider_state,
)
from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialGateway,
)
from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_environment.provider_state import (
    LoginEnvironment,
    refresh_login,
    start_login_environment,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime import environment as turn
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    ConversationKey,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS, CliAgentInfo
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

#: What every synthetic secret carries, and nothing else does.
MARK = "SYNTH459SECRET"
#: What a check plants where it looks, to see that it finds what is there.
CANARY = "SYNTH459CANARY"
#: A file of the guest's writable layer, which the host keeps on its disk.
_PLANTED = "/var/tmp/boundary-canary"
#: The home the snapshot and the runtime belong to; the suite moves HOME.
_REAL_HOME = Path.home()
_SANDBOXES = _REAL_HOME / ".guildbotics/data/msb/sandboxes"
_FAR = 4102444800  # 2100-01-01, in seconds.
_ACCESS = f"{MARK}A459"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_CONTRACT_PROBE") != "1",
        reason="Set GUILDBOTICS_CONTRACT_PROBE=1 to probe the provider CLIs.",
    ),
    pytest.mark.asyncio,
]

_TOOLS = [agent.name for agent in CLI_AGENTS]


def _jwt(claims: dict[str, Any], secret: str) -> str:
    """A JWT whose signature is ``secret``, so the token itself carries it."""

    def segment(value: Any) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode())
        return encoded.rstrip(b"=").decode()

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.{secret}"


def _login(tool: str) -> bytes:
    """A synthetic login of ``tool``, shaped as its own login leaves it."""
    access, refresh, identity = _ACCESS, f"{MARK}R459", f"{MARK}I459"
    if tool == "claude":
        document: Any = {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": _FAR * 1000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    elif tool == "codex":
        account = {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": "account-459",
        }
        document = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _jwt(
                    {"email": "s@example.com", "https://api.openai.com/auth": account},
                    identity,
                ),
                "access_token": _jwt({"exp": _FAR}, access),
                "refresh_token": refresh,
                "account_id": "account-459",
            },
            "last_refresh": "2026-09-01T00:00:00Z",
        }
    elif tool == "grok":
        document = {
            "https://auth.x.ai::00000000-0000-0000-0000-000000000459": {
                "key": access,
                "refresh_token": refresh,
                "expires_at": "2100-01-01T00:00:00Z",
                "auth_mode": "oidc",
            }
        }
    elif tool == "antigravity":
        document = {
            "token": {
                "access_token": access,
                "token_type": "Bearer",
                "refresh_token": refresh,
                "expiry": "2100-01-01T00:00:00Z",
            },
            "auth_method": "consumer",
            "id_token": identity,
        }
    else:
        return (
            "// This file is managed automatically.\n"
            + json.dumps(
                {"authTokens": {"https://github.com:synthetic459": {"token": access}}}
            )
        ).encode()
    return json.dumps(document).encode()


def _on_disk(*roots: Path, since: float = 0.0, mark: str = MARK) -> list[str]:
    """The files under ``roots`` (changed since ``since``) that hold ``mark``."""
    found = []
    for root in roots:
        files = [root] if root.is_file() else root.rglob("*")
        for path in files:
            try:
                if not path.is_file() or path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            # grep streams: the writable layer is a sparse file of gigabytes.
            hit = subprocess.run(
                ["grep", "-qaF", mark, str(path)], capture_output=True, check=False
            )
            if hit.returncode == 0:
                found.append(str(path.resolve()))
    return sorted(set(found))


def _sandboxes() -> set[str]:
    return set(os.listdir(_SANDBOXES)) if _SANDBOXES.is_dir() else set()


@pytest_asyncio.fixture
async def device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[LoginEnvironment]:
    """This device's environment, with a store and a vault of the test's own."""
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(_REAL_HOME))
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    for module in (provider_state, credential_vault):
        monkeypatch.setattr(
            module,
            "get_machine_state_path",
            lambda *parts: tmp_path.joinpath("data", *parts),
        )
    status = device_status()
    if status.refusal or status.snapshot is None or status.declaration is None:
        pytest.skip(f"The agent environment is not ready here: {status.refusal}")
    yield LoginEnvironment(
        snapshot=status.snapshot.path,
        memory_mib=status.declaration.resources.memory_mib,
        cpus=status.declaration.resources.cpus,
        nameservers=status.dns.nameservers,
    )


def _seal(name: str) -> CliAgentInfo:
    tool = next(agent for agent in CLI_AGENTS if agent.name == name)
    provider_state._seal_login(tool, {tool.provision.auth: _login(name)})
    return tool


def _node(script: str, *args: str, environment: str = "") -> str:
    """A shell line that runs ``script`` in Node, out of the shell's reach."""
    encoded = base64.b64encode(script.encode()).decode()
    quoted = " ".join(f"'{arg}'" for arg in args)
    return f'{environment} node -e "$(echo {encoded} | base64 -d)" {quoted}'


async def _sh(environment: Any, script: str, *, timeout: float = 120) -> str:
    process = await environment.run("sh", "-c", script, limit=1 << 22)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        await process.kill()
        raise
    return out.decode(errors="replace") + err.decode(errors="replace")


#: Every process of the guest but this one: its environment and command line.
_PROCESSES = (
    "const fs=require('fs');const mark='SYNTH459'+'SECRET';const hits=[];"
    "for(const p of fs.readdirSync('/proc')){if(!/^[0-9]+$/.test(p)||+p===process.pid)"
    "continue;for(const f of ['environ','cmdline']){try{if(fs.readFileSync("
    "`/proc/${p}/${f}`).includes(mark))hits.push(`${p}/${f}`)}catch(e){}}}"
    "console.log(JSON.stringify(hits))"
)
#: Every file of the guest; the pattern comes in on standard input, so that
#: no command line holds it.
_FILES = (
    "printf '%s%s' SYNTH459 SECRET | grep -rlaF -f - / "
    "--exclude-dir=proc --exclude-dir=sys --exclude-dir=dev; true"
)
#: A request as the tool makes it, through the gateway: ``url``, ``method``,
#: ``stand-in``; what comes back is printed.
_THROUGH_GATEWAY = (
    "const [url,method,stand]=process.argv.slice(1);"
    "fetch(url,{method,headers:{authorization:'Bearer '+stand},"
    "body:method==='GET'?undefined:'{}'})"
    ".then(async r=>console.log(r.status,JSON.stringify([...r.headers]),"
    "await r.text()))"
    ".catch(e=>console.log('failed',String(e.cause||e)))"
)


def _echo(request: httpx.Request) -> httpx.Response:
    """An upstream that echoes the token back, in a header and in the body."""
    authorization = request.headers.get("authorization", "")
    body = json.dumps({"authorization": authorization}).encode()
    return httpx.Response(
        200,
        headers={"x-echo": authorization, "content-type": "application/json"},
        stream=httpx.ByteStream(body),
    )


@pytest.mark.parametrize("name", _TOOLS)
async def test_a_turn_holds_no_real_value_and_is_answered_none(
    device: LoginEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    started = time.time()
    caplog.set_level(logging.DEBUG, logger="guildbotics")
    tool = _seal(name)
    broker = tool.provision.credential_broker
    assert broker is not None
    stand_ins: list[str] = []

    class Echoed(CredentialGateway):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, transport=httpx.MockTransport(_echo), **kwargs)

        def lend(self, tokens: Any, stand_in: str) -> None:
            stand_ins.append(stand_in)
            super().lend(tokens, stand_in)

    monkeypatch.setattr(turn, "CredentialGateway", Echoed)
    work = tmp_path / "work"
    work.mkdir()
    context = AgentExecutionContext(
        person_id="probe",
        run_id="boundary",
        cwd=work,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("probe", name, "manual", "boundary"),
        contract=AccessContract(),
    )
    before = _sandboxes()
    environment = await turn.start_turn_environment(context, name)
    try:
        (sandbox,) = _sandboxes() - before
        # What the checks must find: a file, and a process's environment.
        await environment.write_file(_PLANTED, f"{MARK}C".encode())
        await _sh(
            environment,
            "M=$(printf %s%s SYNTH459 SECRET)C"
            " nohup sleep 600 </dev/null >/dev/null 2>&1 &",
        )
        files = await _sh(environment, _FILES, timeout=300)
        processes = await _sh(environment, _node(_PROCESSES))
        base = environment.spec.env[broker.base_url_env[0]]
        origin = base.removesuffix(broker.base_url_path)
        # A route of one path to the upstream itself, which the gateway's
        # origin stands for.
        method, _, path = next(
            r for r in broker.routes if " /" in r and "*" not in r
        ).partition(" ")
        answered = await _sh(
            environment,
            _node(
                _THROUGH_GATEWAY,
                f"{origin}{path}",
                method,
                stand_ins[-1],
                environment=f"NODE_EXTRA_CA_CERTS={turn._TURN_CAS}"
                if broker.tls
                else "",
            ),
        )
    finally:
        await environment.close()

    # Nothing of the login in the turn, from a shell or a tool's own reads:
    # what the checks find is what they were given to find.
    assert files.split() == [_PLANTED], files
    (planted,) = json.loads(processes.splitlines()[-1])
    assert planted.endswith("/environ"), processes
    # The gateway took the stand-in and answered, the echoed token masked.
    assert answered.startswith("200 "), answered
    assert MARK not in answered and "*" * 8 in answered, answered
    # Nothing of the turn left on this device, nor in what was logged.
    assert sandbox not in _sandboxes()
    planted_here = tmp_path / "canary"
    planted_here.write_text(MARK)
    assert _on_disk(tmp_path, Path(tempfile.gettempdir()), since=started) == [
        str(planted_here.resolve())
    ]
    logging.getLogger("guildbotics").debug(CANARY)  # What the log check must find.
    assert CANARY in caplog.text and MARK not in caplog.text


async def test_the_turns_of_a_command_share_one_microvm(
    device: LoginEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One microVM for the command: each turn runs in its own working
    directory with its own environment, is lent a stand-in (and, over TLS, a
    CA) of its own that the previous turn's no longer opens, and the microVM
    is gone when the command ends."""
    for name in ("antigravity", "claude"):
        _seal(name)
    broker = next(a for a in CLI_AGENTS if a.name == "antigravity").provision
    assert broker.credential_broker is not None
    base_url_env = broker.credential_broker.base_url_env[0]
    stand_ins: list[str] = []

    class Echoed(CredentialGateway):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, transport=httpx.MockTransport(_echo), **kwargs)

        def lend(self, tokens: Any, stand_in: str) -> None:
            stand_ins.append(stand_in)
            super().lend(tokens, stand_in)

    monkeypatch.setattr(turn, "CredentialGateway", Echoed)
    work = tmp_path / "work"
    (work / "package").mkdir(parents=True)

    def context(name: str, cwd: Path) -> AgentExecutionContext:
        return AgentExecutionContext(
            person_id="probe",
            run_id="shared",
            cwd=cwd,
            workspace_root=tmp_path,
            workspace_data_root=tmp_path,
            conversation_key=ConversationKey("probe", name, "manual", "shared"),
            contract=AccessContract(),
            tools=frozenset({"antigravity", "claude"}),
        )

    async def through_gateway(environment: Any, stand_in: str) -> str:
        return await _sh(
            environment,
            _node(
                _THROUGH_GATEWAY,
                f"{environment.spec.env[base_url_env]}/v1internal:loadCodeAssist",
                "POST",
                stand_in,
                environment=f"NODE_EXTRA_CA_CERTS={turn._TURN_CAS}",
            ),
        )

    before = _sandboxes()
    answers: list[str] = []
    async with turn.command_environment():
        first = await turn.start_turn_environment(
            context("antigravity", work), "antigravity"
        )
        try:
            (sandbox,) = _sandboxes() - before
            answers.append(await through_gateway(first, stand_ins[-1]))
            await _sh(first, "echo left-by-the-first-turn > /var/tmp/trace")
        finally:
            await first.close()
        second = await turn.start_turn_environment(
            context("claude", work / "package"), "claude"
        )
        try:
            where = (await _sh(second, "pwd; cat /var/tmp/trace")).split()
            told = await _sh(second, f'printf %s "${base_url_env}"')
        finally:
            await second.close()
        third = await turn.start_turn_environment(
            context("antigravity", work), "antigravity"
        )
        try:
            answers.append(await through_gateway(third, stand_ins[0]))
            answers.append(await through_gateway(third, stand_ins[-1]))
        finally:
            await third.close()
        assert _sandboxes() - before == {sandbox}

    assert sandbox not in _sandboxes()
    assert where[0].endswith("/work/package") and where[1] == "left-by-the-first-turn"
    # The claude turn is not told where antigravity's gateway is.
    assert told == ""
    assert len(set(stand_ins)) == 3
    assert answers[0].startswith("200 "), answers
    assert answers[1].startswith("401 "), answers
    assert answers[2].startswith("200 "), answers


def _probing(tool: CliAgentInfo) -> CliAgentInfo:
    """``tool`` held where it reaches no domain: nothing leaves the device."""
    provision = tool.provision.model_copy(update={"api_domains": ()})
    return tool.model_copy(update={"provision": provision})


#: What each tool runs where its login is held: its refresh, or -- for the
#: one that never refreshes -- a question about its account.
def _asking(tool: CliAgentInfo) -> tuple[str, ...]:
    broker = tool.provision.credential_broker
    assert broker is not None
    return broker.refresh or ("copilot", "--no-auto-update", "-p", "hi")


@pytest.mark.parametrize("name", _TOOLS)
async def test_where_the_login_is_held_none_of_it_reaches_the_disk(
    device: LoginEnvironment, name: str
) -> None:
    """What a power cut would leave of an environment that holds the login:
    its writable layer and logs, as they are on this device's disk."""
    tool = _probing(_seal(name))
    before = _sandboxes()
    environment = await start_login_environment(tool, device)
    try:
        (sandbox,) = _sandboxes() - before
        await environment.write_file(_PLANTED, CANARY.encode())
        command = " ".join(f"'{part}'" for part in _asking(tool))
        await _sh(environment, f"timeout 60 {command} </dev/null; sync", timeout=120)
        left = _on_disk(_SANDBOXES / sandbox)
        seen = _on_disk(_SANDBOXES / sandbox, mark=CANARY)
    finally:
        await environment.close()

    # The disk holds what the environment wrote outside its memory ...
    assert any(path.endswith("upper.ext4") for path in seen), seen
    # ... and none of the login, which stays in its memory.
    assert left == [], left
    assert sandbox not in _sandboxes()


async def test_a_refresh_cancelled_or_timed_out_leaves_nothing(
    device: LoginEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that is still running when it is cancelled, or when its
    time is up, takes its microVM with it."""
    tool = _probing(_seal("claude"))
    broker = tool.provision.credential_broker
    assert broker is not None
    lingering = broker.model_copy(update={"refresh": ("sleep", "600")})
    tool = tool.model_copy(
        update={
            "provision": tool.provision.model_copy(
                update={"credential_broker": lingering}
            )
        }
    )
    before = _sandboxes()

    monkeypatch.setattr(provider_state, "_LOGIN_HOLD_SECONDS", 5.0)
    with pytest.raises(provider_state.CredentialUnavailableError):
        await refresh_login(tool, device, _ACCESS)
    assert _sandboxes() == before

    monkeypatch.setattr(provider_state, "_LOGIN_HOLD_SECONDS", 90.0)
    refreshing = asyncio.create_task(refresh_login(tool, device, _ACCESS))
    for _ in range(600):
        if _sandboxes() - before:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(3)  # The refresh is running inside.
    assert not refreshing.done()
    refreshing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refreshing
    assert _sandboxes() == before


#: An owner that holds a login in a microVM and is then killed.
_OWNER = """
import asyncio, sys
from pathlib import Path
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec, EnvironmentMount, EnvironmentNetwork, guest_home)
from guildbotics.intelligences.agent_environment.status import device_status

async def main():
    status = device_status()
    home = guest_home()
    environment = await AgentEnvironment.start(
        AgentEnvironmentSpec(
            cwd=home, home=home,
            mounts=(EnvironmentMount(f"{home}/.login", None, False),),
            network=EnvironmentNetwork(False, (), (), False, status.dns.nameservers),
            env={}),
        snapshot=str(status.snapshot.path), memory_mib=1024, cpus=1)
    await environment.write_file(f"{home}/.login/login", sys.argv[1].encode())
    await environment.write_file("/var/tmp/boundary-canary", sys.argv[2].encode())
    process = await environment.run("sync", limit=1024)
    await process.communicate()
    print("held", flush=True)
    await asyncio.sleep(600)

asyncio.run(main())
"""


async def test_an_environment_whose_owner_is_killed_goes_with_it(
    device: LoginEnvironment,
) -> None:
    """A killed GuildBotics takes the microVM that held a login with it, and
    nothing of it stays on the disk."""
    before = _sandboxes()
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER, f"{MARK}K459", CANARY],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "held"
        (sandbox,) = _sandboxes() - before
        assert _on_disk(_SANDBOXES / sandbox, mark=CANARY)
        assert _on_disk(_SANDBOXES / sandbox) == []
    finally:
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait()
    for _ in range(100):
        if sandbox not in _sandboxes():
            break
        await asyncio.sleep(0.1)
    assert sandbox not in _sandboxes()


async def test_the_snapshot_holds_no_login(device: LoginEnvironment) -> None:
    """Every environment boots from it, and none writes back into it."""
    assert _on_disk(device.snapshot) == []
