import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from guildbotics.integrations.github import app_manifest

HTTP_NOT_FOUND = 404


def _pem_bytes() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def test_manifest_submission_url_targets_user_or_organization() -> None:
    assert (
        app_manifest.manifest_submission_url() == "https://github.com/settings/apps/new"
    )
    assert (
        app_manifest.manifest_submission_url("acme")
        == "https://github.com/organizations/acme/settings/apps/new"
    )


def test_app_installation_page_url() -> None:
    assert (
        app_manifest.app_installation_page_url("my-bot")
        == "https://github.com/apps/my-bot/installations/new"
    )


def test_build_app_manifest_contains_required_permissions() -> None:
    manifest = app_manifest.build_app_manifest(
        "my-bot", "http://127.0.0.1:8765/github-app/registrations/callback"
    )
    assert manifest["name"] == "my-bot"
    assert manifest["public"] is False
    assert (
        manifest["redirect_url"]
        == "http://127.0.0.1:8765/github-app/registrations/callback"
    )
    assert manifest["default_permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "repository_projects": "write",
        "organization_projects": "write",
        "workflows": "write",
        "metadata": "read",
    }
    # Webhooks stay disabled: GuildBotics polls GitHub instead.
    assert "hook_attributes" not in manifest
    assert json.dumps(manifest)


@pytest.mark.asyncio
async def test_convert_manifest_code_parses_credentials() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            201,
            json={
                "id": 1978826,
                "slug": "my-bot",
                "html_url": "https://github.com/apps/my-bot",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\n...",
                "owner": {"login": "acme"},
            },
        )

    conversion = await app_manifest.convert_manifest_code(
        "tmp-code", transport=httpx.MockTransport(handler)
    )

    request = seen["request"]
    assert request.method == "POST"
    assert request.url.path == "/app-manifests/tmp-code/conversions"
    assert request.headers["Accept"] == "application/vnd.github.v3+json"
    assert conversion.app_id == 1978826
    assert conversion.slug == "my-bot"
    assert conversion.html_url == "https://github.com/apps/my-bot"
    assert conversion.pem.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert conversion.owner_login == "acme"


@pytest.mark.asyncio
async def test_convert_manifest_code_raises_on_error_status() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(HTTP_NOT_FOUND, json={"message": "Not Found"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await app_manifest.convert_manifest_code("expired", transport=transport)


@pytest.mark.asyncio
async def test_list_app_installations_uses_app_jwt() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json=[
                {"id": 86632391, "account": {"login": "acme"}},
                {"id": 12, "account": {}},
            ],
        )

    installations = await app_manifest.list_app_installations(
        "1978826", _pem_bytes(), transport=httpx.MockTransport(handler)
    )

    request = seen["request"]
    assert request.url.path == "/app/installations"
    assert request.headers["Authorization"].startswith("Bearer ")
    assert [i.installation_id for i in installations] == [86632391, 12]
    assert installations[0].account_login == "acme"
    assert installations[1].account_login == ""
