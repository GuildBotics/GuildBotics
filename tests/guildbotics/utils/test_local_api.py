"""The session token reaches only a process that proves it already holds it."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.cli.desktop_commands import run_on_desktop
from guildbotics.hub import host, secret_service, secret_transport
from guildbotics.secrets.hub_client import LocalHubSecretClient, SecretOffer
from guildbotics.utils import local_api
from guildbotics.utils.local_api import (
    PROOF_PATH,
    TOKEN_HEADER,
    LocalApiEndpoint,
    connect_local_api,
    local_api_proof,
)

TOKEN = "session-token"
INSTANCE = "instance"
WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"
SECRET = b"framed-secret-value"
#: A nonce and the genuine proof for it, as an impostor could have recorded.
RECORDED_NONCE = "0" * 64
RECORDED_PROOF = local_api_proof(TOKEN, RECORDED_NONCE, INSTANCE)

#: How each impostor answers the proof request; every other request gets a
#: healthy-looking answer for any workspace, so only the proof can stop it.
IMPOSTORS: dict[str, Callable[[str], tuple[int, bytes]]] = {
    "no_proof_route": lambda nonce: (404, b"{}"),
    "other_key": lambda nonce: (
        200,
        json.dumps({"proof": local_api_proof("other", nonce, INSTANCE)}).encode(),
    ),
    "replayed": lambda nonce: (200, json.dumps({"proof": RECORDED_PROOF}).encode()),
    "other_instance": lambda nonce: (
        200,
        json.dumps({"proof": local_api_proof(TOKEN, nonce, "old")}).encode(),
    ),
    "not_json": lambda nonce: (200, b"ok"),
    "not_an_object": lambda nonce: (200, b"[]"),
    "not_a_string": lambda nonce: (200, b'{"proof": 1}'),
    "non_ascii": lambda nonce: (200, json.dumps({"proof": "é"}).encode()),
    "slow": lambda nonce: (time.sleep(0.5), (200, b"{}"))[1],
}


class _Seen:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []


@pytest.fixture
def machine_state(tmp_path, monkeypatch):
    monkeypatch.setattr(local_api, "endpoint_path", lambda: tmp_path / "app-api.json")
    return tmp_path


@pytest.fixture(params=sorted(IMPOSTORS))
def impostor(request, machine_state, monkeypatch) -> Iterator[_Seen]:
    """A loopback server on the port a stale discovery record still names."""
    answer = IMPOSTORS[request.param]
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.requests.append((self.path, dict(self.headers), body))
            if self.path == PROOF_PATH:
                status, content = answer(json.loads(body)["nonce"])
            else:
                status, content = (
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "service_instance_id": INSTANCE,
                            "workspace": str(machine_state),
                        }
                    ).encode(),
                )
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = do_POST = _serve

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(local_api, "_PROOF_TIMEOUT_SECONDS", 0.2)
    LocalApiEndpoint(
        port=server.server_address[1],
        token=TOKEN,
        service_instance_id=INSTANCE,
        workspace=machine_state,
    ).publish()
    yield seen
    server.shutdown()
    server.server_close()
    thread.join()


def _assert_only_a_nonce_was_sent(seen: _Seen) -> None:
    assert [path for path, _, _ in seen.requests] == [PROOF_PATH]
    for _, headers, body in seen.requests:
        assert TOKEN not in json.dumps(headers) and TOKEN.encode() not in body
        assert SECRET not in body


def test_an_impostor_never_receives_the_token(impostor):
    with connect_local_api(timeout=5.0) as client:
        assert client is None
    _assert_only_a_nonce_was_sent(impostor)


def test_the_cli_runs_locally_instead_of_delegating_to_an_impostor(
    impostor, machine_state
):
    assert run_on_desktop(machine_state, "ask", (), None, "work", machine_state) is None
    _assert_only_a_nonce_was_sent(impostor)


def test_hub_secrets_are_not_handed_to_an_impostor(impostor, monkeypatch):
    monkeypatch.setattr(secret_transport, "DELEGATES_TO_DESKTOP", True)
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)
    offer = SecretOffer("A_TOKEN", 1, SECRET.decode())
    result = LocalHubSecretClient(WORKSPACE_ID).send([offer])
    assert result[0].status == secret_service.DESKTOP_REQUIRED
    _assert_only_a_nonce_was_sent(impostor)


def test_no_record_means_no_connection(machine_state):
    with connect_local_api(timeout=5.0) as client:
        assert client is None


@pytest.fixture
def genuine(machine_state, monkeypatch) -> Iterator[list[httpx.Request]]:
    """The real Local API, reached through its routes in process."""
    app = create_app(session_token=TOKEN, runtime=AppRuntime(EventBus()))
    test_client = TestClient(app, client=("127.0.0.1", 1234))
    instance = test_client.get("/health", headers={TOKEN_HEADER: TOKEN}).json()[
        "service_instance_id"
    ]
    LocalApiEndpoint(
        port=8765, token=TOKEN, service_instance_id=instance, workspace=None
    ).publish()
    requests: list[httpx.Request] = []

    def exchange(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = test_client.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(response.status_code, content=response.content)

    client_type = httpx.Client
    monkeypatch.setattr(
        local_api.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(exchange)),
    )
    yield requests
    test_client.close()


def test_the_genuine_api_proves_itself_before_the_token_is_attached(genuine):
    with connect_local_api(timeout=5.0) as client:
        assert client is not None
        assert client.get("/health").status_code == 200
    proof, health = genuine
    assert proof.url.path == PROOF_PATH and TOKEN_HEADER not in proof.headers
    assert health.headers[TOKEN_HEADER] == TOKEN


def test_a_stale_record_of_the_genuine_api_is_not_trusted(genuine):
    stale = local_api.read_endpoint()
    assert stale is not None
    stale.service_instance_id = "previous-instance"
    stale.publish()
    with connect_local_api(timeout=5.0) as client:
        assert client is None
    assert len(genuine) == 1


def test_each_proof_uses_a_fresh_nonce(genuine):
    for _ in range(2):
        with connect_local_api(timeout=5.0) as client:
            assert client is not None
    nonces = {json.loads(request.content)["nonce"] for request in genuine}
    assert len(nonces) == 2


@pytest.mark.parametrize(
    "nonce", ["", "0" * 63, "0" * 65, "G" * 64, "A" * 64, "0" * 63 + "\n"]
)
def test_the_proof_route_signs_only_a_well_formed_nonce(nonce):
    app = create_app(session_token=TOKEN, runtime=AppRuntime(EventBus()))
    with TestClient(app) as client:
        assert client.post(PROOF_PATH, json={"nonce": nonce}).status_code == 422
