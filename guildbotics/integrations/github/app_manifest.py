"""Client for GitHub's "create a GitHub App from a manifest" flow.

The flow cannot be fully headless: the manifest is posted to github.com from
the user's browser, and GitHub redirects back with a one-time code once the
user clicks "Create GitHub App". This module holds the provider knowledge of
that flow: the manifest payload, the submission / installation page URLs, the
code-to-credentials conversion API, and installation listing with an app JWT.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from guildbotics.integrations.github.async_client import raise_for_status_with_text
from guildbotics.integrations.github.github_utils import create_github_app_jwt

GITHUB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
HOMEPAGE_URL = "https://github.com/GuildBotics/GuildBotics"
REQUEST_TIMEOUT_SECONDS = 15.0

# Permissions required by GuildBotics workflows (see README "Using a GitHub
# App"): repository Contents / Issues / Projects / Pull requests plus
# organization Projects, all read & write.
DEFAULT_APP_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "repository_projects": "write",
    "organization_projects": "write",
    "metadata": "read",
}


class AppManifestConversion(BaseModel):
    """Credentials returned by the manifest code conversion API."""

    app_id: int
    slug: str
    html_url: str = ""
    pem: str
    owner_login: str = ""


class AppInstallation(BaseModel):
    """One installation of a GitHub App."""

    installation_id: int
    account_login: str = ""


def manifest_submission_url(organization: str = "") -> str:
    """Return the github.com page that accepts the manifest form POST."""
    if organization:
        return f"{GITHUB_URL}/organizations/{organization}/settings/apps/new"
    return f"{GITHUB_URL}/settings/apps/new"


def app_installation_page_url(slug: str) -> str:
    """Return the page where the user installs the app on repositories."""
    return f"{GITHUB_URL}/apps/{slug}/installations/new"


def build_app_manifest(app_name: str, redirect_url: str) -> dict:
    """Build the manifest posted to GitHub's app creation page.

    Webhooks are omitted on purpose: GuildBotics polls GitHub instead of
    receiving events.
    """
    return {
        "name": app_name,
        "url": HOMEPAGE_URL,
        "public": False,
        "redirect_url": redirect_url,
        "default_permissions": dict(DEFAULT_APP_PERMISSIONS),
    }


async def convert_manifest_code(
    code: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> AppManifestConversion:
    """Exchange the temporary manifest code for the created app's credentials."""
    async with _api_client(transport) as client:
        response = await client.post(f"/app-manifests/{code}/conversions")
        await raise_for_status_with_text(response)
        data = response.json()
    owner = data.get("owner") or {}
    return AppManifestConversion(
        app_id=int(data["id"]),
        slug=str(data["slug"]),
        html_url=str(data.get("html_url") or ""),
        pem=str(data["pem"]),
        owner_login=str(owner.get("login") or ""),
    )


async def list_app_installations(
    app_id: str,
    private_key_pem: bytes,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[AppInstallation]:
    """List the app's installations, authenticated with an app JWT."""
    jwt_token = create_github_app_jwt(app_id, private_key_pem)
    async with _api_client(transport) as client:
        response = await client.get(
            "/app/installations",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        await raise_for_status_with_text(response)
        data = response.json()
    installations: list[AppInstallation] = []
    for item in data or []:
        account = item.get("account") or {}
        installations.append(
            AppInstallation(
                installation_id=int(item["id"]),
                account_login=str(account.get("login") or ""),
            )
        )
    return installations


def _api_client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GITHUB_API_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        transport=transport,
        headers={
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GuildBotics/1.0",
        },
    )
