"""Borrow two tools through the Rust host window in one command VM."""

import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import traceback
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def host(name, payload):
    with urlopen(
        Request(
            configuration["url"] + "/" + name,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + configuration["token"],
                "Content-Type": "application/json",
            },
        ),
        timeout=10,
    ) as response:
        return json.load(response)["result"]


def begin(tool, cwd):
    return host("begin_turn", {"tool": tool, "cwd": cwd})


def end(turn):
    assert host("end_turn", {"turn_grant": turn["turn_grant"]})["refusal"] == ""


def token(turn):
    path = Path(turn["home"]) / ".gemini/antigravity-cli/antigravity-oauth-token"
    return json.loads(path.read_bytes())["token"]["access_token"]


def through_gateway(turn, credential):
    env = turn["env"]
    with urlopen(
        Request(env["CLOUD_CODE_URL"] + "/test", headers={"Authorization": f"Bearer {credential}"}),
        context=ssl.create_default_context(cafile=env["SSL_CERT_FILE"]),
        timeout=10,
    ) as response:
        return response.read()


def report_error(kind, value, trace):
    traceback.print_exception(kind, value, trace, file=sys.stdout)


sys.excepthook = report_error
configuration = json.load(sys.stdin)
Path("package").mkdir()
first = begin("antigravity", "/work")
old = token(first)
assert through_gateway(first, old) == b"test response"
subprocess.run(
    ["sh", "-ec", "echo first > /var/tmp/guildbotics-turn-marker"],
    cwd=first["cwd"],
    env={**os.environ, **first["env"]},
    check=True,
    timeout=10,
)
end(first)
second = begin("claude", "/work/package")
assert "CLOUD_CODE_URL" not in second["env"]
output = subprocess.check_output(
    ["sh", "-ec", "pwd; cat /var/tmp/guildbotics-turn-marker"],
    cwd=second["cwd"],
    env={**os.environ, **second["env"]},
    timeout=10,
)
assert output == b"/work/package\nfirst\n"
end(second)

third = begin("antigravity", "/work")
current = token(third)
assert old != current
try:
    through_gateway(third, old)
except HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("the previous turn's credential is still active")
assert through_gateway(third, current) == b"test response"
end(third)
print("shared VM")
