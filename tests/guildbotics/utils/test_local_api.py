"""This machine's credential reaches only a process that proves it knows the
credential's digest."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    API_PREFIX,
    PORT_ENV,
    PROOF_PATH,
    client_tokens,
    connect_local_api,
    keep_client_token,
    local_api_proof,
    new_token,
    owner_token_path,
    parse_token,
    server_port,
)

TOKEN_ID, DIGEST, TOKEN = new_token()
WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"
SECRET = b"framed-secret-value"
#: A nonce and the genuine proof for it, as an impostor could have recorded.
RECORDED_NONCE = "0" * 64
RECORDED_PROOF = local_api_proof(DIGEST, RECORDED_NONCE, TOKEN_ID)

#: How each impostor answers the proof request; every other request gets a
#: healthy-looking answer for any workspace, so only the proof can stop it.
IMPOSTORS: dict[str, Callable[[str], tuple[int, bytes]]] = {
    "no_proof_route": lambda nonce: (404, b"{}"),
    "other_key": lambda nonce: (
        200,
        json.dumps({"proof": local_api_proof("0" * 64, nonce, TOKEN_ID)}).encode(),
    ),
    "replayed": lambda nonce: (200, json.dumps({"proof": RECORDED_PROOF}).encode()),
    "not_json": lambda nonce: (200, b"ok"),
    "not_an_object": lambda nonce: (200, b"[]"),
    "not_a_string": lambda nonce: (200, b'{"proof": 1}'),
    "non_ascii": lambda nonce: (200, json.dumps({"proof": "é"}).encode()),
    "slow": lambda nonce: (200, b"{}"),
}


class _Seen:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.release = threading.Event()
        self.finished = threading.Event()


@pytest.fixture
def machine_state(tmp_path):
    return tmp_path


def _hold_token(port: int) -> None:
    path = owner_token_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TOKEN + "\n", encoding="utf-8")


@pytest.fixture(params=sorted(IMPOSTORS))
def impostor(request, machine_state, monkeypatch) -> Iterator[_Seen]:
    """A loopback server that took the server's port while it was down."""
    answer = IMPOSTORS[request.param]
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            try:
                self._answer()
            except BrokenPipeError, ConnectionResetError, ConnectionAbortedError:
                # The slow proof deliberately outlives the client's read timeout.
                pass
            finally:
                seen.finished.set()

        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.requests.append((self.path, dict(self.headers), body))
            if request.param == "slow":
                assert seen.release.wait(10)
            if self.path == API_PREFIX + PROOF_PATH:
                status, content = answer(json.loads(body)["nonce"])
            else:
                status, content = (
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "service_instance_id": "instance",
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

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv(PORT_ENV, str(server.server_address[1]))
    _hold_token(server.server_address[1])
    try:
        yield seen
    finally:
        seen.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def _assert_only_a_nonce_was_sent(seen: _Seen) -> None:
    seen.release.set()
    assert seen.finished.wait(10), "the proof handler did not finish"
    assert [path for path, _, _ in seen.requests] == [API_PREFIX + PROOF_PATH]
    for _, headers, body in seen.requests:
        assert TOKEN not in json.dumps(headers) and TOKEN.encode() not in body
        assert json.loads(body).keys() == {"token_id", "nonce"}
        assert SECRET not in body


def test_an_impostor_never_receives_the_token(impostor):
    with connect_local_api(timeout=5.0) as client:
        assert client is None
    _assert_only_a_nonce_was_sent(impostor)


def test_an_impostor_receives_neither_token_a_machine_holds(impostor):
    consent = new_token()[2]
    keep_client_token(server_port(), consent)
    with connect_local_api(timeout=5.0, consent=True) as client:
        assert client is None
    impostor.release.set()
    assert impostor.finished.wait(10), "the proof handler did not finish"
    assert impostor.requests
    for path, headers, body in impostor.requests:
        assert path == API_PREFIX + PROOF_PATH
        for token in (TOKEN, consent):
            assert token not in json.dumps(headers) and token.encode() not in body
        assert json.loads(body).keys() == {"token_id", "nonce"}


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


def test_no_credential_means_no_connection(machine_state):
    with connect_local_api(timeout=5.0) as client:
        assert client is None


@pytest.fixture
def genuine(machine_state, monkeypatch) -> Iterator[list[httpx.Request]]:
    """The real server, reached through its routes in process, holding the
    owner token file it wrote."""
    app = create_app(runtime=AppRuntime(EventBus()))
    test_client = TestClient(app, base_url="http://127.0.0.1:8765")
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


def test_the_genuine_server_proves_itself_before_the_token_is_attached(genuine):
    with connect_local_api(timeout=5.0) as client:
        assert client is not None
        assert client.get("/health").status_code == 200
    proof, health = genuine
    assert proof.url.path == API_PREFIX + PROOF_PATH
    assert "authorization" not in proof.headers
    token = owner_token_path(8765).read_text("utf-8").strip()
    assert health.headers["authorization"] == f"Bearer {token}"


def test_a_token_the_server_does_not_know_is_not_sent(genuine):
    _hold_token(8765)
    with connect_local_api(timeout=5.0) as client:
        assert client is None
    assert [request.url.path for request in genuine] == [API_PREFIX + PROOF_PATH]


def test_an_owner_file_left_by_an_earlier_server_does_not_hide_the_consent(genuine):
    # The port now leads to a server this machine holds a consent token for,
    # while a server that once ran here left its own owner file behind.
    consent = owner_token_path(8765).read_text("utf-8").strip()
    keep_client_token(8765, consent)
    _hold_token(8765)
    with connect_local_api(timeout=5.0, consent=True) as client:
        assert client is not None
        assert client.get("/health").status_code == 200
    stale, proven, health = genuine
    assert [stale.url.path, proven.url.path] == [API_PREFIX + PROOF_PATH] * 2
    assert "authorization" not in stale.headers
    assert "authorization" not in proven.headers
    assert health.headers["authorization"] == f"Bearer {consent}"


def test_the_keychain_is_not_read_when_the_owner_file_is_taken(genuine, monkeypatch):
    from guildbotics.utils import keychain

    def unreachable() -> None:
        pytest.fail("the keychain was read")

    monkeypatch.setattr(keychain, "system_keychain", unreachable)
    with connect_local_api(timeout=5.0, consent=True) as client:
        assert client is not None


def test_only_this_machines_server_is_reached_without_consent(genuine, monkeypatch):
    # A caller handing over this machine's paths or secrets must not reach the
    # server the consent token is for, however the port leads there.
    from guildbotics.utils import keychain

    keep_client_token(8765, owner_token_path(8765).read_text("utf-8").strip())
    _hold_token(8765)

    def unreachable() -> None:
        pytest.fail("the keychain was read")

    monkeypatch.setattr(keychain, "system_keychain", unreachable)
    with connect_local_api(timeout=5.0) as client:
        assert client is None
    assert [request.url.path for request in genuine] == [API_PREFIX + PROOF_PATH]


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
    app = create_app(runtime=AppRuntime(EventBus()))
    token_id, _ = parse_token(owner_token_path(8765).read_text("utf-8")) or ("", "")
    client = TestClient(app, base_url="http://127.0.0.1:8765" + API_PREFIX)
    answer = client.post(PROOF_PATH, json={"token_id": token_id, "nonce": nonce})
    assert answer.status_code == 422


def test_the_proof_route_signs_only_for_a_known_token():
    app = create_app(runtime=AppRuntime(EventBus()))
    client = TestClient(app, base_url="http://127.0.0.1:8765" + API_PREFIX)
    answer = client.post(PROOF_PATH, json={"token_id": TOKEN_ID, "nonce": "0" * 64})
    assert answer.status_code == 401


def test_the_machine_offers_its_owner_file_then_its_consent_token(machine_state):
    assert list(client_tokens(8765, consent=True)) == []
    keep_client_token(8765, "consent-token")
    assert list(client_tokens(8765, consent=True)) == ["consent-token"]
    assert list(client_tokens(8765, consent=False)) == []
    _hold_token(8765)
    assert list(client_tokens(8765, consent=True)) == [TOKEN, "consent-token"]
    assert list(client_tokens(8765, consent=False)) == [TOKEN]
    assert list(client_tokens(8766, consent=True)) == []


@pytest.mark.parametrize(
    ("value", "port"), [(None, 8765), ("", 8765), (" 9000 ", 9000), ("65535", 65535)]
)
def test_the_port_is_the_machines_setting(monkeypatch, value, port):
    if value is None:
        monkeypatch.delenv(PORT_ENV, raising=False)
    else:
        monkeypatch.setenv(PORT_ENV, value)
    assert server_port() == port


@pytest.mark.parametrize("value", ["0", "65536", "-1", "http", "80a"])
def test_a_port_setting_that_names_no_port_is_refused(monkeypatch, value):
    monkeypatch.setenv(PORT_ENV, value)
    with pytest.raises(ValueError, match=PORT_ENV):
        server_port()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "gbt",
        "gbt__secret",
        "bvt_0123456789abcdef_s",
        "gbt_0123456789ABCDEF_s",
        "gbt_0123456789abcde_s",
        "gbt_0123456789abcdef_",
    ],
)
def test_text_that_is_no_token_is_not_parsed(text):
    assert parse_token(text) is None


def test_a_token_parses_to_its_id_and_digest():
    assert parse_token(TOKEN) == (TOKEN_ID, DIGEST)


def test_a_machine_without_a_usable_keychain_holds_no_consent_token(monkeypatch):
    from keyring.errors import NoKeyringError

    from guildbotics.utils import keychain

    class Unusable:
        def get_password(self, service: str, username: str) -> str | None:
            raise NoKeyringError("no backend")

    monkeypatch.setattr(keychain, "system_keychain", Unusable)
    assert list(client_tokens(8765, consent=True)) == []
    with connect_local_api(timeout=5.0, consent=True) as client:
        assert client is None
