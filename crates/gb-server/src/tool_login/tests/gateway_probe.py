"""Inspect a turn's stand-in and exercise each admitted gateway route."""

import json
from pathlib import Path
import ssl
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

for probe in json.load(sys.stdin):
    root = Path(probe["root"])
    env = probe["env"]
    assert "synthetic-device-login" not in json.dumps(env)
    for path in root.rglob("*"):
        if path.is_file():
            assert b"synthetic-device-login" not in path.read_bytes()
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
            Request(probe["base"] + path, headers=headers, method=method),
            context=context,
            timeout=10,
        ) as response:
            assert response.status == 200
            assert "synthetic-device-login" not in response.headers["x-echo"]
            assert b"synthetic-device-login" not in response.read()
    try:
        urlopen(Request(probe["base"] + "/unlisted", headers=headers), context=context, timeout=10)
    except HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError(f"{probe['name']} forwarded a route outside its allowlist")

print("gateway-probe-ok")
