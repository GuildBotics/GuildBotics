"""macOS CLI -> authenticated Desktop -> Hub keychain, with real wire bodies."""

from functools import partial

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
from guildbotics.utils import local_api
from guildbotics.utils.local_api import (
    API_PREFIX,
    PROOF_PATH,
    new_token,
    owner_token_path,
    parse_token,
)
from tests.guildbotics.app_api.owner import ORIGIN, owner_token

WORKSPACE = "11111111-2222-3333-4444-555555555555"
PROOF = API_PREFIX + PROOF_PATH
VALUE = "synthetic-credential\r\n日本語\n"


@pytest.mark.parametrize("operation", ["list", "send", "receive"])
@pytest.mark.parametrize(
    "workspace_id",
    [
        "../../workspace/secrets",
        "../commands",
        "not-a-uuid",
        "urn:uuid:11111111-2222-3333-4444-555555555555",
    ],
)
def test_invalid_workspace_is_refused_before_discovery(
    monkeypatch, operation, workspace_id
):
    def forbidden():
        pytest.fail("an invalid ID must not reach Desktop discovery or HTTP")

    monkeypatch.setattr(secret_transport, "connect_local_api", forbidden)
    monkeypatch.setattr(secret_transport, "DELEGATES_TO_DESKTOP", True)
    with pytest.raises(host.InvalidWorkspaceIdError):
        secret_transport.execute(operation, workspace_id)


@pytest.fixture
def desktop(monkeypatch):
    host.create_hub()
    host.create_workspace_repository(WORKSPACE)
    # The Desktop's server keeps its owner token where the CLI of this machine
    # reads it.
    app = create_app(runtime=AppRuntime(EventBus()))
    client = TestClient(app, base_url=ORIGIN + API_PREFIX, client=("127.0.0.1", 1234))
    monkeypatch.setattr(secret_transport, "DELEGATES_TO_DESKTOP", True)
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
        local_api.httpx,
        "Client",
        partial(httpx.Client, transport=httpx.MockTransport(exchange)),
    )
    yield client, requests
    client.close()


def test_ssh_cli_and_local_client_use_desktop_without_workspace_match(
    desktop, monkeypatch
):
    # The Hub's workspace is not the one the Desktop has selected: the client
    # names it by its ID, and nothing compares the two.
    client, requests = desktop
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
    transfers = [
        r for r in requests if r.url.path.startswith(API_PREFIX + "/hub/secrets/")
    ]
    proofs = [r for r in requests if r.url.path == PROOF]
    assert len(transfers) == len(proofs) == 4
    bearer = f"Bearer {owner_token(client.app)}"
    assert all(r.headers["Authorization"] == bearer for r in transfers)
    assert not any("Authorization" in r.headers for r in proofs)
    assert all(r.url.host == "127.0.0.1" for r in requests)


@pytest.mark.parametrize("failure", ["absent", "stale", "unauthorized", "disconnected"])
def test_desktop_unavailable_never_reads_or_writes_keychain(
    desktop, monkeypatch, failure
):
    client, requests = desktop
    token_file = owner_token_path(client.app.state.auth.port)
    if failure == "absent":
        token_file.unlink()
    elif failure == "stale":
        # A token of an earlier server, which this one never issued.
        token_file.write_text(new_token()[2])
    elif failure == "unauthorized":
        # The id this server knows, with a secret it does not.
        token_id = parse_token(owner_token(client.app))[0]  # type: ignore[index]
        token_file.write_text(f"gbt_{token_id}_wrong-secret")
    else:

        def disconnected(request):
            raise httpx.ConnectError("private-error-text", request=request)

        monkeypatch.setattr(
            local_api.httpx,
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
    assert all(r.url.path == PROOF for r in requests)


def test_endpoint_refuses_nonloopback_and_malformed_requests(desktop, caplog):
    client, _ = desktop
    headers = {"Authorization": f"Bearer {owner_token(client.app)}"}
    url = f"/hub/secrets/{WORKSPACE}/receive"
    response = client.post(url, content=VALUE, headers=headers)
    assert response.status_code == 400
    assert VALUE not in response.text and VALUE not in caplog.text
    with TestClient(
        client.app, base_url=ORIGIN + API_PREFIX, client=("192.0.2.1", 1234)
    ) as remote:
        assert remote.post(url, headers=headers).status_code == 403
    assert (
        client.post("/hub/secrets/not-a-workspace/list", headers=headers).status_code
        == 400
    )


def test_post_is_not_retried_after_lost_response(desktop, monkeypatch):
    client, _ = desktop
    calls = []

    def exchange(request):
        calls.append(request.url.path)
        if request.url.path == PROOF:
            response = client.post(
                PROOF_PATH, content=request.content, headers=dict(request.headers)
            )
            return httpx.Response(response.status_code, content=response.content)
        raise httpx.ReadError("private-error-text", request=request)

    monkeypatch.setattr(
        local_api.httpx,
        "Client",
        partial(
            httpx.Client,
            transport=httpx.MockTransport(exchange),
        ),
    )
    result = LocalHubSecretClient(WORKSPACE).send([SecretOffer("A_TOKEN", 1, VALUE)])
    assert result[0].status == "desktop_required"
    assert calls == [PROOF, f"{API_PREFIX}/hub/secrets/{WORKSPACE}/receive"]
