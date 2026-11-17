"""Borrow two tools through the Rust host window in one command VM."""

import json
import os
from pathlib import Path
import ssl
import subprocess
from shlex import split
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from guildbotics.guest.host_client import command_window


def token(turn):
    return json.loads(
        subprocess.check_output(
            split(turn.env["GROK_AUTH_PROVIDER_COMMAND"]),
            env={**os.environ, **turn.env},
        )
    )["access_token"]


def through_gateway(turn, credential):
    env = turn.env
    with urlopen(
        Request(
            env["GROK_CLI_CHAT_PROXY_BASE_URL"].removesuffix("/v1") + "/test",
            headers={"Authorization": f"Bearer {credential}"},
        ),
        context=ssl.create_default_context(cafile=env.get("SSL_CERT_FILE")),
        timeout=10,
    ) as response:
        return response.read()


async def main(context):
    client = command_window()
    assert client is not None
    work = Path.cwd()
    vm_url = json.loads(context.pipe)

    def vm():
        with urlopen(vm_url, timeout=10) as response:
            return response.read()

    first = await client.begin_turn("grok", str(work))
    initial_vm = vm()
    old = token(first)
    assert through_gateway(first, old) == b"test response"
    subprocess.run(
        ["sh", "-ec", "echo first > /var/tmp/guildbotics-turn-marker"],
        cwd=first.cwd,
        env={**os.environ, **first.env},
        check=True,
        timeout=10,
    )
    assert await client.end_turn(first.turn_grant) == ""
    second = await client.begin_turn("claude", str(work / "package"))
    assert vm() == initial_vm
    assert "GROK_CLI_CHAT_PROXY_BASE_URL" not in second.env
    output = subprocess.check_output(
        ["sh", "-ec", "pwd; cat /var/tmp/guildbotics-turn-marker"],
        cwd=second.cwd,
        env={**os.environ, **second.env},
        timeout=10,
    )
    assert output.decode().splitlines() == [str(work / "package"), "first"]
    assert await client.end_turn(second.turn_grant) == ""

    third = await client.begin_turn("grok", str(work))
    assert vm() == initial_vm
    current = token(third)
    assert old != current
    try:
        through_gateway(third, old)
    except HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("the previous turn's credential is still active")
    assert through_gateway(third, current) == b"test response"
    assert await client.end_turn(third.turn_grant) == ""
    return "shared VM"
