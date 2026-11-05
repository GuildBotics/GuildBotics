"""Inspect a turn's stand-in and exercise each admitted gateway route."""

import json
from pathlib import Path
import ssl
import subprocess
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from guildbotics.guest.host_client import command_window


def assert_no_login(turn):
    mark = b"synthetic-device-" + b"login"
    marker = Path("/var/tmp/guildbotics-scan-canary")
    marker.write_bytes(mark)
    with subprocess.Popen(["sleep", "60"], env={**turn.env, "PROBE": mark.decode()}) as child:
        try:
            files = subprocess.run(
                ["grep", "-rlaF", "-f", "-", "/", "--exclude-dir=proc",
                 "--exclude-dir=sys", "--exclude-dir=dev"],
                input=mark + b"\n", capture_output=True, timeout=60,
            )
            assert files.returncode == 0, files.stderr
            assert files.stdout.decode().splitlines() == [str(marker)], files.stdout
            hits = []
            for process in Path("/proc").iterdir():
                if process.name.isdecimal():
                    for name in ("environ", "cmdline"):
                        path = process / name
                        try:
                            if mark in path.read_bytes():
                                hits.append(path)
                        except (FileNotFoundError, PermissionError, ProcessLookupError):
                            pass
            assert hits == [Path(f"/proc/{child.pid}/environ")], hits
        finally:
            child.terminate()
            marker.unlink()
    assert mark.decode() not in json.dumps(turn.env)


def check_routes(probe, turn):
    root = Path(turn.home) / probe["state_root"]
    env = turn.env
    assert turn.mounts[str(root)] is False
    for entry in probe["persisted"]:
        assert turn.mounts[str(root / entry)] is False
    base = env[probe["base_env"]].removesuffix(probe["base_suffix"])
    if name := probe["stand_in_env"]:
        token = env[name]
    elif name := probe["stand_in_command"]:
        token = json.loads(subprocess.check_output(env[name], shell=True))["access_token"]
    else:
        value = json.loads((root / probe["auth"]).read_bytes())
        for key in probe["access_path"]:
            value = next(iter(value.values())) if key == "*" else value[key]
        token = value

    context = ssl.create_default_context(cafile=env.get("SSL_CERT_FILE"))
    headers = {"Authorization": f"Bearer {token}"}
    for method, path in probe["routes"]:
        with urlopen(
            Request(base + path, headers=headers, method=method),
            context=context,
            timeout=10,
        ) as response:
            assert response.status == 200
            assert "synthetic-device-" + "login" not in response.headers["x-echo"]
            assert b"synthetic-device-" + b"login" not in response.read()
    try:
        urlopen(Request(base + "/unlisted", headers=headers), context=context, timeout=10)
    except HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError(f"{probe['name']} forwarded a route outside its allowlist")


async def main(context):
    client = command_window()
    assert client is not None
    fixture = json.loads(context.pipe)
    vm = None
    for probe in fixture["probes"]:
        turn = await client.begin_turn(probe["name"], os.getcwd())
        try:
            with urlopen(fixture["vm"], timeout=10) as response:
                running = response.read()
            vm = running if vm is None else vm
            assert vm == running
            assert_no_login(turn)
            check_routes(probe, turn)
        finally:
            assert await client.end_turn(turn.turn_grant) == ""
    return "gateway-probe-ok"
