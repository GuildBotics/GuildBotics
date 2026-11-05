"""Drive the snapshot's real CLI processes and return observable results."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

from guildbotics.guest.acp import config_values
from guildbotics.guest.codex import _config_arguments, _gateway_overrides
from guildbotics.guest.gateway_relays import relays, trust_turn
from guildbotics.guest.usage_probe import EnvironmentProcess

PROMPT = "Synthetic fixture."


async def run(*argv, timeout=25):
    child = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    process = EnvironmentProcess(child)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout)
        return output.decode(errors="replace")
    finally:
        if child.returncode is None:
            await process.kill()


@asynccontextmanager
async def rpc(*argv):
    process = EnvironmentProcess(
        await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    request_id = 0

    async def request(method, params, *, notification=False):
        nonlocal request_id
        request_id += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()
        if notification:
            return None
        async with asyncio.timeout(12):
            while line := await process.stdout.readline():
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if isinstance(response, dict) and response.get("id") == request_id:
                    return response
        raise RuntimeError(f"{argv[0]} closed its RPC stream during {method}")

    try:
        yield request
    finally:
        await process.kill()


def result(response):
    assert "result" in response, response
    return response["result"]


async def claude(_):
    os.environ["IS_SANDBOX"] = "1"
    await run("claude", "-p", PROMPT, "--max-turns", "1", "--tools", "")
    # Separate the two observations in the same recorder.
    try:
        urlopen(os.environ["CONTRACT_HTTP_URL"] + "/fixture-usage", timeout=5).close()
    except HTTPError as error:
        assert error.code == 403
    await run("claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence")
    return {}


async def grok(_):
    async with rpc("grok", "--no-auto-update", "agent", "stdio") as request:
        initialized = result(await request(
            "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "probe", "title": "probe", "version": "1"},
            },
        ))
        (external,) = [
            method for method in initialized["authMethods"]
            if method.get("_meta", {}).get("external_provider")
        ]
        result(await request("authenticate", {"methodId": external["id"]}))
        result(await request("session/new", {"cwd": os.getcwd(), "mcpServers": []}))
        billing = await request("_x.ai/billing", {})
    return {"billing": billing["error"]["message"]}


async def codex(_):
    options = _config_arguments(_gateway_overrides(SimpleNamespace(env=os.environ)))
    await run("codex", "exec", *options, "--skip-git-repo-check", PROMPT, timeout=40)
    async with rpc("codex", "app-server", *options) as request:
        result(await request(
            "initialize",
            {"clientInfo": {"name": "probe", "title": "probe", "version": "1"}},
        ))
        await request("initialized", {}, notification=True)
        account = result(await request("account/read", {}))["account"]
        await request("account/rateLimits/read", {})
    return {"account": account}


async def copilot(models):
    output = {}
    async with rpc("copilot", "--acp", "--no-auto-update", "--no-remote-export") as request:
        result(await request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}))
        result(await request("authenticate", {"methodId": "copilot-login"}))
        for model in models:
            session = result(await request("session/new", {"cwd": os.getcwd(), "mcpServers": []}))
            session_id = session["sessionId"]
            if config_values(session).get("model") != model:
                configured = result(await request(
                    "session/set_config_option",
                    {"sessionId": session_id, "configId": "model", "value": model},
                ))
                assert config_values(configured).get("model") == model, configured
            output[model] = await request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": PROMPT}]},
            )
    return output


async def antigravity_quota(_):
    async with relays():
        started = monotonic()
        output = await run(
            "agy", "--print", PROMPT, "--output-format", "stream-json",
            "--print-timeout", "60s", timeout=65,
        )
        elapsed = monotonic() - started
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    reported = next((event["result"] for event in events if event["event"] == "result"), None)
    assert reported is not None, output
    return {"seconds": elapsed, "result": reported}


async def antigravity_refresh(argv):
    await run(*argv, timeout=45)
    return {}


async def main():
    cases = {
        "claude": claude,
        "grok": grok,
        "codex": codex,
        "copilot": copilot,
        "antigravity-quota": antigravity_quota,
        "antigravity-refresh": antigravity_refresh,
    }
    os.environ.update(trust_turn({"env": os.environ})["env"])
    work = Path.home() / "work"
    work.mkdir()
    os.chdir(work)
    print(json.dumps(await cases[sys.argv[1]](sys.argv[2:])))


asyncio.run(main())
