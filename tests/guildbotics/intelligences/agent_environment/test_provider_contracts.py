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
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from contract_recorder import Recorder, json_answer

from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.spec import (
    GUEST_HOST_ALIAS,
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
    guest_path,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

#: The home the snapshot was built with; the suite's own fixtures move HOME.
_REAL_HOME = Path.home()
STAND_IN = "guildbotics-stand-in-SYNTHETIC-459"
_COPILOT_STAND_IN = "gho_SYNTHETIC459standin"
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
        recorder: Recorder, state_root: str, env: dict[str, str], *, proxy: bool = True
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
                **recorder.ca_environment(_CA),
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
    answered = await guest.sh(
        "(cat; sleep 20) | timeout 30 grok --no-auto-update agent stdio",
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
    boot,
) -> None:
    recorder = Recorder()
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(recorder, ".codex", {"CODEX_HOME": f"{home}/.codex"})
    claims = {
        "email": "synthetic@example.com",
        "exp": int(time.time()) + 10**8,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": "account-459",
        },
    }
    stand_in = _jwt(claims)
    await guest.write(
        f"{home}/.codex/auth.json",
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _jwt(claims),
                    "access_token": stand_in,
                    "refresh_token": "",
                    "account_id": "account-459",
                },
                "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ),
    )
    await guest.write(
        f"{home}/.codex/config.toml",
        f'chatgpt_base_url = "{recorder.url}/backend-api/"\n'
        'model_provider = "guildbotics"\n'
        "[model_providers.guildbotics]\n"
        'name = "OpenAI"\n'
        f'base_url = "{recorder.url}/backend-api/codex"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n",
    )

    await guest.sh("timeout 60 codex exec --skip-git-repo-check 'Synthetic fixture.'")
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
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            },
        )
    )
    await guest.sh("(cat; sleep 10) | timeout 25 codex app-server", stdin=rpc.encode())

    responses = recorder.requests("/backend-api/codex/responses")
    assert responses and {s.method for s in responses} == {"POST"}
    assert _bearer(recorder, "/backend-api/codex/responses") == {f"Bearer {stand_in}"}
    assert all("chatgpt-account-id" in s.headers for s in responses)
    assert _bearer(recorder, "/backend-api/wham/usage") == {f"Bearer {stand_in}"}
    assert recorder.carrying(stand_in) == {GUEST_HOST_ALIAS}, recorder.seen


async def test_copilot_follows_its_api_urls_and_answers_quota_from_the_user_api(
    boot,
) -> None:
    def answer(host: str, method: str, path: str):
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
        return None

    recorder = Recorder(answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".copilot",
        {
            "COPILOT_HOME": f"{home}/.copilot",
            # Copilot takes only a GitHub-shaped token.
            "COPILOT_GITHUB_TOKEN": _COPILOT_STAND_IN,
            "COPILOT_DEBUG_GITHUB_API_URL": recorder.url,
            "COPILOT_API_URL": recorder.url,
        },
    )

    await guest.sh(
        "timeout 60 copilot --no-auto-update -p 'Synthetic fixture.' --allow-all-tools"
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

    for path in ("/copilot_internal/user", "/models"):
        assert _bearer(recorder, path) == {f"Bearer {_COPILOT_STAND_IN}"}, recorder.seen
    assert '"quotaSnapshots"' in quota, quota
    # Telemetry bypasses the API URLs, but carries no credential.
    assert recorder.carrying(_COPILOT_STAND_IN) == {GUEST_HOST_ALIAS}, recorder.seen


_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


async def test_antigravity_needs_its_userinfo_redirected_beside_its_cloud_code_url(
    boot,
) -> None:
    def answer(host: str, method: str, path: str):
        if path.startswith("/v1internal:loadCodeAssist"):
            return json_answer(
                {
                    "currentTier": {"id": "standard-tier"},
                    "cloudaicompanionProject": "synthetic-project-459",
                }
            )
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
        if path.startswith("/v1internal:") and ":tabChat" not in path:
            return json_answer({})
        return None

    recorder = Recorder(answer)
    home = guest_path(_REAL_HOME.resolve())
    guest = await boot(
        recorder,
        ".gemini",
        {"CLOUD_CODE_URL": recorder.tls_url},
        # A proxy would draw www.googleapis.com away from the relay below.
        proxy=False,
    )
    await guest.write(
        f"{home}/.gemini/antigravity-cli/antigravity-oauth-token",
        json.dumps(
            {
                "token": {
                    "access_token": STAND_IN,
                    "token_type": "Bearer",
                    "expiry": "2099-01-01T00:00:00Z",
                },
                "auth_method": "consumer",
            }
        ),
    )
    # The userinfo URL is fixed in the binary: a relay inside the guest is
    # the only way to it that does not route every tool through a proxy.
    relay = (
        "require('net').createServer(c=>{const u=require('net').connect("
        f"{recorder.tls_port},'{GUEST_HOST_ALIAS}');c.pipe(u);u.pipe(c);"
        "c.on('error',()=>u.destroy());u.on('error',()=>c.destroy())})"
        ".listen(443,'127.0.0.2')"
    )
    await guest.sh(
        "printf '127.0.0.2 www.googleapis.com\\n127.0.0.2 lh3.googleusercontent.com\\n'"
        f' >> /etc/hosts; (node -e "{relay}" >/dev/null 2>&1 &); sleep 1'
    )

    await guest.sh(
        "timeout 70 agy --print 'Synthetic fixture.' --output-format stream-json",
        timeout=100,
    )

    userinfo = [
        s
        for s in recorder.requests("/oauth2/v2/userinfo")
        if s.host == "www.googleapis.com"
    ]
    assert userinfo, recorder.seen
    assert _bearer(recorder, "/v1internal:loadCodeAssist") == {f"Bearer {STAND_IN}"}
    assert recorder.requests("/v1internal:tabChat"), recorder.seen
    # Seen through a proxy, the credential also goes straight to
    # play.googleapis.com (telemetry, which a turn can do without): the one
    # destination a turn must never be let through to. Nothing else does.
    assert recorder.carrying(STAND_IN) == {GUEST_HOST_ALIAS, "www.googleapis.com"}
