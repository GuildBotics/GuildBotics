"""The connection contract each provider CLI keeps with a brokered login.

Skipped unless ``GUILDBOTICS_CONTRACT_PROBE=1``, on a device whose agent
environment is ready (point ``GUILDBOTICS_CONFIG_DIR`` at a workspace with a
built snapshot). Each test boots the snapshot's pinned CLI in a real microVM,
hands it a synthetic stand-in the way a turn would get one, points it at a
recorder on the host, and asserts what #459's gateway relies on: which
requests reach the pointed URL carrying the stand-in, and that nothing that
carries it goes anywhere else. Run it when a pinned version changes; a
failure here is a change in the provider's contract, not a flaky test. It uses
no account and sends nothing off the device.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from contract_recorder import Recorder, json_answer

from guildbotics.intelligences.agent_environment import provider_state
from guildbotics.intelligences.agent_environment.provider_state import LentLogin
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.spec import (
    GUEST_HOST_ALIAS,
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
    guest_path,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime import environment
from guildbotics.intelligences.agent_runtime.codex import (
    _config_arguments,
    _gateway_overrides,
)
from guildbotics.intelligences.cli_agents import cli_agent_info
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

#: The home the snapshot was built with; the suite's own fixtures move HOME.
_REAL_HOME = Path.home()
STAND_IN = "guildbotics-stand-in-SYNTHETIC-459"
#: Where the guest finds the run's CA; outside every state root.
_CA = "/etc/contract-probe-ca.pem"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_CONTRACT_PROBE") != "1",
        reason="Set GUILDBOTICS_CONTRACT_PROBE=1 to probe the provider CLIs.",
    ),
    pytest.mark.asyncio,
]


class Guest:
    """One microVM with the tool's state root in memory and a work directory."""

    def __init__(self, environment: AgentEnvironment) -> None:
        self.environment = environment
        self.home = environment.spec.home

    async def write(self, path: str, data: bytes | str) -> None:
        await self.sh(f"mkdir -p '{path.rsplit('/', 1)[0]}'")
        await self.environment.write_file(
            path, data.encode() if isinstance(data, str) else data
        )

    async def sh(self, script: str, *, stdin: bytes = b"", timeout: float = 90) -> str:
        process = await self.environment.run("sh", "-c", script, limit=1 << 22)
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            await process.kill()
            raise
        return out.decode(errors="replace") + err.decode(errors="replace")


@pytest_asyncio.fixture
async def boot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[object]:
    # The snapshot is this device's and the workspace's: undo the suite's
    # isolation of both, and read the workspace from GUILDBOTICS_CONFIG_DIR.
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(_REAL_HOME))
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    status = device_status()
    if status.refusal or status.snapshot is None or status.declaration is None:
        pytest.skip(f"The agent environment is not ready here: {status.refusal}")
    started: list[AgentEnvironment] = []

    async def start(
        recorder: Recorder,
        state_root: str,
        env: dict[str, str],
        *,
        proxy: bool = True,
        ca: bool = True,
    ) -> Guest:
        home = guest_path(_REAL_HOME.resolve())
        work = Path(tempfile.mkdtemp())
        spec = AgentEnvironmentSpec(
            cwd=f"{home}/work",
            home=home,
            mounts=(
                EnvironmentMount(f"{home}/{state_root}", None, False),
                EnvironmentMount(f"{home}/work", work, False),
            ),
            network=EnvironmentNetwork(
                False,
                (),
                (recorder.port, recorder.tls_port),
                False,
                status.dns.nameservers,
            ),
            env={
                **(recorder.proxy_environment() if proxy else {}),
                **(recorder.ca_environment(_CA) if ca else {}),
                **env,
            },
        )
        environment = await AgentEnvironment.start(
            spec,
            snapshot=str(status.snapshot.path),
            memory_mib=status.declaration.resources.memory_mib,
            cpus=status.declaration.resources.cpus,
        )
        started.append(environment)
        guest = Guest(environment)
        await guest.write(_CA, recorder.ca_pem)
        return guest

    yield start
    for environment in started:
        await environment.close()


def _bearer(recorder: Recorder, prefix: str) -> set[str]:
    return {seen.authorization for seen in recorder.requests(prefix)}


async def test_claude_sends_inference_to_its_base_url_and_usage_straight_to_anthropic(
    boot,
) -> None:
    recorder = Recorder()
    guest = await boot(
        recorder,
        ".claude",
        {
            "CLAUDE_CONFIG_DIR": f"{guest_path(_REAL_HOME.resolve())}/.claude",
            "ANTHROPIC_BASE_URL": recorder.url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "IS_SANDBOX": "1",
        },
    )
    await guest.write(
        f"{guest.home}/.claude/.credentials.json",
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": STAND_IN,
                    "expiresAt": int(time.time() * 1000) + 10**10,
                    "scopes": ["user:inference", "user:profile"],
                    "subscriptionType": "max",
                }
            }
        ),
    )

    await guest.sh("claude -p 'Synthetic fixture.' --max-turns 1 --tools ''")

    assert _bearer(recorder, "/v1/messages") == {f"Bearer {STAND_IN}"}
    # The account profile ignores the base URL. A turn is not let through to
    # api.anthropic.com, where the stand-in authenticates nothing anyway.
    direct = {
        (x.method, x.path) for x in recorder.requests() if x.host != GUEST_HOST_ALIAS
    }
    assert direct == {("GET", "/api/oauth/profile")}, recorder.seen

    await guest.sh("claude -p /usage --output-format json --no-session-persistence")

    # `/usage` does not follow the base URL: it is read where the login is.
    assert "api.anthropic.com" in recorder.carrying(STAND_IN), recorder.seen


async def test_grok_sends_everything_to_its_chat_proxy_and_refuses_billing(
    boot,
) -> None:
    recorder = Recorder()
    home = guest_path(_REAL_HOME.resolve())
    helper = json.dumps({"access_token": STAND_IN, "expires_in": 10**8})
    guest = await boot(
        recorder,
        ".grok",
        {
            "GROK_HOME": f"{home}/.grok",
            "GROK_AUTH_PATH": f"{home}/.grok/auth/auth.json",
            "GROK_AUTH_PROVIDER_COMMAND": f"echo '{helper}'",
            "GROK_CLI_CHAT_PROXY_BASE_URL": f"{recorder.url}/v1",
        },
    )

    def rpc(*messages: tuple[int, str, dict[str, object]]) -> bytes:
        return "".join(
            json.dumps({"jsonrpc": "2.0", "id": n, "method": m, "params": p}) + "\n"
            for n, m, p in messages
        ).encode()

    initialize = (
        1,
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "probe", "title": "probe", "version": "1"},
        },
    )
    advertised = _replies(
        await guest.sh(
            "(cat; sleep 5) | timeout 15 grok agent stdio", stdin=rpc(initialize)
        )
    )[1]["result"]["authMethods"]
    # What the adapter keys on: the method the auth provider command adds.
    (lent,) = [m for m in advertised if m.get("_meta", {}).get("external_provider")]
    # Grok Build serves requests side by side: the session is asked for only
    # once the login has had time to be taken, as the adapter awaits it.
    answered = await guest.sh(
        '(read -r a; read -r b; printf \'%s\\n\' "$a" "$b"; sleep 5; cat; sleep 20)'
        " | timeout 35 grok --no-auto-update agent stdio",
        stdin=rpc(
            initialize,
            (2, "authenticate", {"methodId": lent["id"]}),
            (3, "session/new", {"cwd": f"{home}/work", "mcpServers": []}),
            (4, "_x.ai/billing", {}),
        ),
    )
    replies = _replies(answered)
    assert "result" in replies[2] and "result" in replies[3], answered

    authenticated = [
        s for s in recorder.requests("/v1/") if s.path != "/v1/login-config"
    ]
    assert {s.authorization for s in authenticated} == {f"Bearer {STAND_IN}"}
    assert {"/v1/user", "/v1/settings", "/v1/models"} <= {
        s.path.split("?")[0] for s in authenticated
    }
    assert recorder.carrying(STAND_IN) == {GUEST_HOST_ALIAS}, recorder.seen
    # An external login cannot read billing: usage is read where the login is.
    assert replies[4]["error"]["message"] == "Authentication required", answered


def _replies(output: str) -> dict[int, dict[str, object]]:
    """The JSON-RPC replies among the lines a CLI printed, by id."""
    replies: dict[int, dict[str, object]] = {}
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("id"), int):
            replies[message["id"]] = message
    return replies


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.c3RhbmQtaW4"


async def test_codex_follows_a_provider_of_its_own_and_its_chatgpt_base_url(
    boot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a turn of Codex holds, and how it is pointed, are the adapter's
    own: the stand-in files a synthetic login lends, and its configuration."""
    codex = cli_agent_info("codex")
    broker = codex.provision.credential_broker
    assert broker is not None
    claims = {
        "email": "synthetic@example.com",
        "exp": int(time.time()) + 10**8,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": "account-459",
        },
    }
    login = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt(claims),
            "access_token": _jwt(claims),
            "refresh_token": "REFRESH-SYNTHETIC-459",
            "account_id": "account-459",
        },
        "last_refresh": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        provider_state,
        "_unsealed_login",
        lambda tool: {codex.provision.auth: json.dumps(login).encode()},
    )
    lent = LentLogin(codex, None)  # type: ignore[arg-type]
    recorder = Recorder()
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(recorder, ".codex", {"CODEX_HOME": f"{home}/.codex"})
    for name, data in lent.stand_in_files().items():
        await guest.write(f"{home}/.codex/{name}", data)
    spec = SimpleNamespace(
        env=dict.fromkeys(broker.base_url_env, recorder.url + broker.base_url_path)
    )
    configured = shlex.join(_config_arguments(_gateway_overrides(spec)))

    await guest.sh(
        f"timeout 60 codex exec {configured} --skip-git-repo-check 'Synthetic fixture.'"
    )
    rpc = "".join(
        json.dumps(message) + "\n"
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "probe", "title": "probe", "version": "1"}
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/read", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "account/rateLimits/read",
                "params": {},
            },
        )
    )
    answered = await guest.sh(
        f"(cat; sleep 10) | timeout 25 codex app-server {configured}",
        stdin=rpc.encode(),
    )

    # The stand-in is a login Codex takes as its account's.
    account = _replies(answered)[2]["result"]["account"]
    assert (account["email"], account["planType"]) == ("synthetic@example.com", "plus")
    responses = recorder.requests("/backend-api/codex/responses")
    assert responses and {s.method for s in responses} == {"POST"}
    assert _bearer(recorder, "/backend-api/codex/responses") == {
        f"Bearer {lent.stand_in}"
    }
    assert all("chatgpt-account-id" in s.headers for s in responses)
    assert _bearer(recorder, "/backend-api/wham/usage") == {f"Bearer {lent.stand_in}"}
    assert recorder.carrying(lent.stand_in) == {GUEST_HOST_ALIAS}, recorder.seen
    # Nothing it holds tries to refresh.
    assert not recorder.requests("/oauth/token"), recorder.seen
    assert "auth.openai.com" not in recorder.connects(), recorder.seen
    # What a turn needs of it is what the gateway forwards.
    needed = {
        (s.method, s.path.split("?")[0])
        for prefix in ("/backend-api/codex/", "/backend-api/wham/usage")
        for s in recorder.requests(prefix)
        if "analytics" not in s.path
    }
    assert needed <= {tuple(route.split(" ", 1)) for route in broker.routes}


async def test_codex_refreshes_a_login_told_it_has_expired_by_listing_models(
    boot,
) -> None:
    """The refresh GuildBotics makes Codex run, on the login as the refresh
    hands it over: its access token claiming an expiry that has passed."""
    codex = cli_agent_info("codex")
    broker = codex.provision.credential_broker
    assert broker is not None
    stale = _jwt({"exp": int(time.time()) + 3600, "who": "stale"})
    fresh = _jwt({"exp": int(time.time()) + 864000, "who": "fresh"})

    def answer(host: str, method: str, path: str):
        if path == "/oauth/token":
            token = {"access_token": fresh, "id_token": fresh}
            return json_answer({**token, "refresh_token": "NEW-REFRESH-459"})
        return None

    recorder = Recorder(answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".codex",
        {
            "CODEX_HOME": f"{home}/.codex",
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": f"{recorder.url}/oauth/token",
        },
    )
    login = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": stale,
            "access_token": stale,
            "refresh_token": "OLD-REFRESH-459",
            "account_id": "account-459",
        },
        "last_refresh": "2026-01-01T00:00:00Z",
    }
    auth = f"{home}/.codex/{codex.provision.auth}"
    await guest.write(auth, provider_state._expired(broker, json.dumps(login).encode()))

    await guest.sh(shlex.join(broker.refresh))

    (refreshing,) = recorder.requests("/oauth/token")
    assert refreshing.method == "POST"
    left = {codex.provision.auth: await guest.environment.read_file(auth)}
    token, expires = provider_state._account_login(broker, left, codex.provision.auth)
    assert token == fresh and expires > time.time() + 800000


def _copilot_answer(host: str, method: str, path: str):
    """What Copilot needs answered to start: the account and its policy, and
    models whose endpoints send inference each of its ways."""
    if path.startswith("/copilot_internal/user"):
        return json_answer(
            {
                "login": "synthetic459",
                "id": 459,
                "copilot_plan": "individual",
                "chat_enabled": True,
                "endpoints": {
                    "api": "https://api.individual.githubcopilot.com",
                    "telemetry": "https://telemetry.individual.githubcopilot.com",
                },
                "quota_snapshots": {"chat": {"unlimited": True}},
            }
        )
    if path.startswith("/copilot_internal/managed_settings"):
        return json_answer({})
    if path.startswith("/models"):
        return json_answer({"data": [_model(*m) for m in _COPILOT_MODELS]})
    return None


#: A model for each way Copilot sends inference: its supported endpoints.
_COPILOT_MODELS = (
    ("gpt-5.2-codex", "/responses", "OpenAI"),
    ("claude-sonnet-4.5", "/v1/messages", "Anthropic"),
    ("gpt-5-mini", "/chat/completions", "OpenAI"),
)


def _model(name: str, endpoint: str, vendor: str) -> dict[str, object]:
    return {
        "id": name,
        "name": name,
        "vendor": vendor,
        "version": "1",
        "object": "model",
        "preview": False,
        "model_picker_enabled": True,
        "policy": {"state": "enabled"},
        "supported_endpoints": [endpoint],
        "capabilities": {
            "type": "chat",
            "family": name,
            "supports": {"streaming": True, "tool_calls": True},
            "limits": {"max_prompt_tokens": 100000, "max_output_tokens": 8000},
        },
    }


async def test_copilot_reaches_github_and_its_api_through_the_gateway(
    boot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn as GuildBotics sets it up: the stand-in in the variable a
    synthetic login lends, and both API URLs at the gateway."""
    copilot = cli_agent_info("copilot")
    broker = copilot.provision.credential_broker
    assert broker is not None
    login = b'// comment\n{"authTokens": {"https://github.com:s": {"token": "gho_R"}}}'
    monkeypatch.setattr(
        provider_state, "_unsealed_login", lambda _: {copilot.provision.auth: login}
    )
    lent = LentLogin(copilot, None)  # type: ignore[arg-type]
    recorder = Recorder(_copilot_answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".copilot",
        {
            "COPILOT_HOME": f"{home}/.copilot",
            **dict.fromkeys(broker.base_url_env, recorder.url),
            **lent.stand_in_environment(),
        },
    )

    for name, _endpoint, _vendor in _COPILOT_MODELS:
        await guest.sh(
            "timeout 60 copilot --no-auto-update -p 'Synthetic fixture.'"
            f" --allow-all-tools --model {name}"
        )
    initialize = {"protocolVersion": 1, "clientCapabilities": {}}
    answered = await guest.sh(
        "(cat; sleep 15) | timeout 25 copilot --acp --no-auto-update",
        stdin="".join(
            json.dumps({"jsonrpc": "2.0", "id": n, "method": m, "params": p}) + "\n"
            for n, m, p in (
                (1, "initialize", initialize),
                (2, "authenticate", {"methodId": "copilot-login"}),
                (3, "session/new", {"cwd": f"{home}/work", "mcpServers": []}),
            )
        ).encode(),
    )

    replies = _replies(answered)
    assert "result" in replies[2] and "result" in replies[3], answered
    # Every credentialed request is one the gateway forwards; nothing else
    # goes anywhere but the gateway.
    authenticated = {
        (s.method, s.path.split("?")[0]) for s in recorder.requests() if s.authorization
    }
    assert all(broker.origin(*request) for request in authenticated), authenticated
    assert {("POST", endpoint) for _, endpoint, _ in _COPILOT_MODELS} <= authenticated
    assert recorder.carrying(lent.stand_in) == {GUEST_HOST_ALIAS}, recorder.seen


async def test_copilot_answers_its_quota_from_the_login_it_leaves(boot) -> None:
    """Where the login is held, the tool reads it from the file its login
    left, comments and all, and its quota from the user API."""
    recorder = Recorder(_copilot_answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".copilot",
        {
            "COPILOT_HOME": f"{home}/.copilot",
            # Moved here only to record what the login is sent with.
            "COPILOT_DEBUG_GITHUB_API_URL": recorder.url,
            "COPILOT_API_URL": recorder.url,
        },
    )
    await guest.write(
        f"{home}/.copilot/config.json",
        "// This file is managed automatically.\n"
        + json.dumps(
            {
                "authTokens": {"https://github.com:synthetic459": {"token": "gho_R4"}},
                "lastLoggedInUser": {
                    "host": "https://github.com",
                    "login": "synthetic459",
                },
                "loggedInUsers": [
                    {"host": "https://github.com", "login": "synthetic459"}
                ],
            }
        ),
    )

    def frame(message: object) -> bytes:
        body = json.dumps(message).encode()
        return b"Content-Length: %d\r\n\r\n" % len(body) + body

    quota = await guest.sh(
        "(cat; sleep 10) | timeout 25 copilot --headless --stdio --no-auto-update",
        stdin=frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "connect",
                "params": {
                    "supportedTaskKinds": [],
                    "clientInfo": {"editorName": "probe", "editorVersion": "1"},
                },
            }
        )
        + frame(
            {"jsonrpc": "2.0", "id": 2, "method": "account.getQuota", "params": {}}
        ),
    )

    assert '"quotaSnapshots"' in quota, quota
    assert _bearer(recorder, "/copilot_internal/user") == {"Bearer gho_R4"}


_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _antigravity_answer(host: str, method: str, path: str):
    """What Antigravity needs answered to run a turn: an account, its
    profile and picture, and a model; the inference is refused."""
    if path.startswith("/v1internal:loadCodeAssist"):
        return json_answer(
            {
                "currentTier": {"id": "standard-tier"},
                "cloudaicompanionProject": "synthetic-project-459",
            }
        )
    if path.startswith("/v1internal:fetchAvailableModels"):
        return json_answer({"models": {"gemini-3-flash": {"displayName": "Gemini"}}})
    if path.startswith("/oauth2/v2/userinfo"):
        return json_answer(
            {
                "id": "459",
                "email": "synthetic@example.com",
                "picture": "https://lh3.googleusercontent.com/a/synthetic",
            }
        )
    if host == "lh3.googleusercontent.com":
        return 200, {"content-type": "image/png"}, _PIXEL
    if path == "/token":
        return json_answer(
            {
                "access_token": "NEW-ACCESS-459",
                "expires_in": 3599,
                "token_type": "Bearer",
            }
        )
    if path.startswith("/v1internal:") and ":streamGenerateContent" not in path:
        return json_answer({})
    return None


_ANTIGRAVITY_LOGIN = {
    "token": {
        "access_token": "REAL-SYNTHETIC-459",
        "token_type": "Bearer",
        "refresh_token": "REFRESH-SYNTHETIC-459",
        "expiry": "2099-01-01T00:00:00Z",
    },
    "auth_method": "consumer",
    "id_token": "ID-SYNTHETIC-459",
}


async def test_antigravity_reaches_its_api_and_its_userinfo_through_the_gateway(
    boot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn as GuildBotics sets it up: the stand-in files a synthetic login
    lends, CLOUD_CODE_URL over TLS, the gateway's CA trusted beside the
    system's, and the userinfo host relayed to the gateway."""
    tool = cli_agent_info("antigravity")
    broker = tool.provision.credential_broker
    assert broker is not None
    monkeypatch.setattr(
        provider_state,
        "_unsealed_login",
        lambda _: {tool.provision.auth: json.dumps(_ANTIGRAVITY_LOGIN).encode()},
    )
    lent = LentLogin(tool, None)  # type: ignore[arg-type]
    recorder = Recorder(_antigravity_answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".gemini",
        {"CLOUD_CODE_URL": recorder.tls_url, "SSL_CERT_FILE": environment._TURN_CAS},
        # A proxy would draw the relayed hosts away from the relay.
        proxy=False,
        ca=False,
    )
    for name, data in lent.stand_in_files().items():
        await guest.write(f"{home}/.gemini/{name}", data)
    await environment._trust(guest.environment, recorder.ca_pem)
    # The picture is reached directly from a turn; here, where nothing leaves
    # the device, it is relayed to the recorder too.
    relay = await environment._relay(
        guest.environment,
        (*broker.relayed_hosts, *broker.turn_domains),
        recorder.tls_port,
    )

    await guest.sh(
        "timeout 70 agy --print 'Synthetic fixture.' --output-format stream-json",
        timeout=100,
    )
    await relay.kill()

    userinfo = recorder.requests("/oauth2/v2/userinfo")
    assert {s.host for s in userinfo} == {"www.googleapis.com"}, recorder.seen
    assert _bearer(recorder, "/v1internal:") == {f"Bearer {lent.stand_in}"}
    assert recorder.requests("/v1internal:streamGenerateContent"), recorder.seen
    assert recorder.carrying(lent.stand_in) == {GUEST_HOST_ALIAS, "www.googleapis.com"}
    # What a turn needs of it is what the gateway forwards, to where.
    for seen in recorder.requests():
        if seen.authorization:
            origin = broker.origin(seen.method, seen.path.split("?")[0])
            assert origin is not None, seen
            expected = "daily-cloudcode-pa.googleapis.com"
            host = GUEST_HOST_ALIAS if origin.endswith(expected) else origin[8:]
            assert seen.host == host, seen


async def test_antigravity_refreshes_a_login_told_it_has_expired_reading_its_usage(
    boot,
) -> None:
    tool = cli_agent_info("antigravity")
    broker = tool.provision.credential_broker
    assert broker is not None
    recorder = Recorder(_antigravity_answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(recorder, ".gemini", {"CLOUD_CODE_URL": recorder.tls_url})
    auth = f"{home}/.gemini/{tool.provision.auth}"
    login = json.dumps(_ANTIGRAVITY_LOGIN).encode()
    await guest.write(auth, provider_state._expired(broker, login))

    await guest.sh(shlex.join(broker.refresh))

    assert [s.host for s in recorder.requests("/token")][:1] == [
        "oauth2.googleapis.com"
    ]
    left = {tool.provision.auth: await guest.environment.read_file(auth)}
    token, expires = provider_state._account_login(broker, left, tool.provision.auth)
    assert token == "NEW-ACCESS-459" and expires > time.time() + 3000
