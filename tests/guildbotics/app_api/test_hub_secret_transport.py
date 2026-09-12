"""macOS CLI -> authenticated Desktop -> Hub keychain, with real wire bodies."""

from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.cli.hub import hub
from guildbotics.hub import host, secret_host, secret_service, secret_transport
from guildbotics.hub.connection import HubEndpoint
from guildbotics.secrets.hub_client import (
    LocalHubSecretClient,
    RemoteHubSecretClient,
    SecretOffer,
)
from guildbotics.utils.local_api import LocalApiEndpoint

WORKSPACE = "11111111-2222-3333-4444-555555555555"
TOKEN = "test-session"
HEADERS = {"X-GuildBotics-Session-Token": TOKEN}
VALUE = "synthetic-credential\r\n日本語\n"


@pytest.fixture
def desktop(monkeypatch):
    host.create_hub()
    host.create_workspace_repository(WORKSPACE)
    app = create_app(session_token=TOKEN, runtime=AppRuntime(EventBus()))
    client = TestClient(app, client=("127.0.0.1", 1234))
    endpoint = LocalApiEndpoint(
        port=8765,
        token=TOKEN,
        pid=1,
        service_instance_id=client.get("/health", headers=HEADERS).json()[
            "service_instance_id"
        ],
        workspace=None,
    )
    monkeypatch.setattr(secret_transport, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(secret_transport, "read_endpoint", lambda: endpoint)
    requests = []

    def exchange(request):
        requests.append(request)
        response = client.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(response.status_code, content=response.content)

    monkeypatch.setattr(
        secret_transport,
        "httpx",
        SimpleNamespace(
            Client=partial(httpx.Client, transport=httpx.MockTransport(exchange)),
            HTTPError=httpx.HTTPError,
        ),
    )
    yield client, endpoint, requests
    client.close()


def test_ssh_cli_and_local_client_use_desktop_without_workspace_match(
    desktop, monkeypatch
):
    _, endpoint, requests = desktop
    assert endpoint.workspace is None
    local = LocalHubSecretClient(WORKSPACE)
    runner = CliRunner()

    def ssh(endpoint, arguments, payload=b""):
        result = runner.invoke(hub, arguments, input=payload)
        assert result.exit_code == 0, result.output
        assert VALUE not in result.stderr
        return result.stdout_bytes

    monkeypatch.setattr("guildbotics.secrets.hub_client.run_hub_stream", ssh)
    remote = RemoteHubSecretClient(HubEndpoint(host="mac-hub"), WORKSPACE)
    assert remote.send([SecretOffer("A_TOKEN", 1, VALUE)])[0].status == "stored"
    assert local.index().generations == {"A_TOKEN": 1}
    assert remote.fetch(["A_TOKEN"])[0].value == VALUE
    assert local.fetch(["A_TOKEN"])[0].value == VALUE
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 4
    assert all(r.headers["X-GuildBotics-Session-Token"] == TOKEN for r in posts)
    assert all(r.url.host == "127.0.0.1" for r in posts)


@pytest.mark.parametrize("failure", ["absent", "stale", "unauthorized", "disconnected"])
def test_desktop_unavailable_never_reads_or_writes_keychain(
    desktop, monkeypatch, failure
):
    _, endpoint, requests = desktop
    if failure == "absent":
        monkeypatch.setattr(secret_transport, "read_endpoint", lambda: None)
    elif failure == "stale":
        endpoint.service_instance_id = "old-instance"
    elif failure == "unauthorized":
        endpoint.token = "wrong-token"
    else:

        def disconnected(request):
            raise httpx.ConnectError("private-error-text", request=request)

        monkeypatch.setattr(
            secret_transport.httpx,
            "Client",
            partial(
                httpx.Client,
                transport=httpx.MockTransport(disconnected),
            ),
        )

    def forbidden(*args, **kwargs):
        pytest.fail("an unavailable Desktop must not fall back to the keychain")

    monkeypatch.setattr(secret_host, "_keychain", forbidden)
    local = LocalHubSecretClient(WORKSPACE)
    index = local.index()
    assert not index.available and not index.locked
    assert index.error_code == secret_service.DESKTOP_REQUIRED
    assert (
        local.send([SecretOffer("A_TOKEN", 1, VALUE)])[0].status == "desktop_required"
    )
    assert local.fetch(["A_TOKEN"])[0].status == "desktop_required"
    assert secret_host.generations(WORKSPACE) == {}
    assert not any(r.method == "POST" for r in requests)


def test_endpoint_refuses_unauthenticated_nonloopback_and_malformed_requests(
    desktop, caplog
):
    client, _, _ = desktop
    url = f"/hub/secrets/{WORKSPACE}/receive"
    assert client.post(url, content=VALUE).status_code == 401
    response = client.post(url, content=VALUE, headers=HEADERS)
    assert response.status_code == 400
    assert VALUE not in response.text and VALUE not in caplog.text
    with TestClient(client.app, client=("192.0.2.1", 1234)) as remote:
        assert remote.post(url, headers=HEADERS).status_code == 403
    assert (
        client.post("/hub/secrets/not-a-workspace/list", headers=HEADERS).status_code
        == 400
    )


def test_post_is_not_retried_after_lost_response(desktop, monkeypatch):
    calls = []

    def exchange(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, json={"service_instance_id": desktop[1].service_instance_id}
            )
        raise httpx.ReadError("private-error-text", request=request)

    monkeypatch.setattr(
        secret_transport.httpx,
        "Client",
        partial(
            httpx.Client,
            transport=httpx.MockTransport(exchange),
        ),
    )
    result = LocalHubSecretClient(WORKSPACE).send([SecretOffer("A_TOKEN", 1, VALUE)])
    assert result[0].status == "desktop_required"
    assert calls == ["GET", "POST"]
