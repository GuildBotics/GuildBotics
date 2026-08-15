"""GitHub App auto-registration flow used by the desktop member setup GUI.

The flow is a semi-automatic browser round trip: the GUI starts a
registration, the user's browser posts the app manifest to github.com and
clicks "Create GitHub App", GitHub redirects back to the local API with a
one-time code, and this module converts the code into credentials. The GUI
then polls the registration until the user has installed the app and the
installation ID could be detected.

Registrations are held in memory only; the PEM is additionally written to a
key file so the ordinary member-save path (``github_private_key_path``)
persists it like a manually downloaded key.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, computed_field

from guildbotics.editions.simple.setup_service import (
    SetupServiceError,
    SimplePersonSetupService,
)
from guildbotics.integrations.github import app_manifest

REGISTRATION_TTL_SECONDS = 30 * 60
GITHUB_APP_NAME_MAX_LENGTH = 34

STATUS_PENDING = "pending"
STATUS_CONVERTED = "converted"
STATUS_INSTALLED = "installed"


class GitHubAppRegistrationInfo(BaseModel):
    """Public view of a registration, shared with the app API response model."""

    state: str
    status: str = STATUS_PENDING
    app_name: str
    slug: str = ""
    app_id: int | None = None
    html_url: str = ""
    github_username: str = ""
    git_email: str = ""
    private_key_path: str = ""
    installation_id: int | None = None
    installation_check_error: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def installation_page_url(self) -> str:
        if not self.slug:
            return ""
        return app_manifest.app_installation_page_url(self.slug)


class GitHubAppRegistration(GitHubAppRegistrationInfo):
    """State of one in-flight GitHub App registration."""

    organization: str
    callback_url: str
    key_dir: Path
    created_at: float = Field(default_factory=time.time)
    pem: str = ""

    def info_dump(self) -> dict:
        """Dump only the fields shared with the public view."""
        return self.model_dump(include=set(GitHubAppRegistrationInfo.model_fields))


class GitHubAppRegistrationService:
    """Hold in-flight registrations and drive the manifest flow."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._registrations: dict[str, GitHubAppRegistration] = {}
        self._transport = transport

    def start(
        self,
        *,
        app_name: str,
        organization: str,
        callback_url: str,
        key_dir: Path,
    ) -> GitHubAppRegistration:
        name = app_name.strip()
        if not name or len(name) > GITHUB_APP_NAME_MAX_LENGTH:
            raise SetupServiceError(
                "invalid_github_app_name",
                "GitHub App name must be 1-34 characters.",
            )
        self._purge_expired()
        registration = GitHubAppRegistration(
            state=secrets.token_urlsafe(32),
            app_name=name,
            organization=organization.strip(),
            callback_url=callback_url,
            key_dir=key_dir,
        )
        self._registrations[registration.state] = registration
        return registration

    def get(self, state: str) -> GitHubAppRegistration:
        self._purge_expired()
        registration = self._registrations.get(state)
        if registration is None:
            raise SetupServiceError(
                "github_app_registration_not_found",
                "GitHub App registration was not found or has expired.",
            )
        return registration

    def manifest_form(self, state: str) -> tuple[str, str]:
        """Return the github.com submission URL and the manifest JSON to post."""
        registration = self.get(state)
        url = app_manifest.manifest_submission_url(registration.organization)
        manifest = app_manifest.build_app_manifest(
            registration.app_name, registration.callback_url
        )
        return f"{url}?state={registration.state}", json.dumps(manifest)

    async def complete(self, state: str, code: str) -> GitHubAppRegistration:
        """Convert the callback code into credentials and store them."""
        registration = self.get(state)
        if registration.status != STATUS_PENDING:
            # A browser reload replays the callback; the one-time code cannot
            # be converted twice, so keep the already-stored result.
            return registration
        conversion = await app_manifest.convert_manifest_code(
            code, transport=self._transport
        )
        registration.slug = conversion.slug
        registration.app_id = conversion.app_id
        registration.html_url = conversion.html_url
        registration.pem = conversion.pem
        registration.private_key_path = str(self._write_key_file(registration))
        registration.github_username = f"{conversion.slug}[bot]"
        registration.git_email = await self._resolve_bot_email(conversion.slug)
        registration.status = STATUS_CONVERTED
        return registration

    async def check_installation(self, state: str) -> GitHubAppRegistration:
        """Detect the app installation and capture its installation ID."""
        registration = self.get(state)
        if registration.status != STATUS_CONVERTED or registration.app_id is None:
            return registration
        try:
            installations = await app_manifest.list_app_installations(
                str(registration.app_id),
                registration.pem.encode(),
                transport=self._transport,
            )
        except (httpx.HTTPError, ValueError) as exc:
            # The GUI polls this; a transient GitHub error must not abort the
            # flow, so surface it on the registration instead of raising.
            registration.installation_check_error = str(exc)
            return registration
        registration.installation_check_error = ""
        if installations:
            registration.installation_id = max(
                installation.installation_id for installation in installations
            )
            registration.status = STATUS_INSTALLED
        return registration

    def _write_key_file(self, registration: GitHubAppRegistration) -> Path:
        registration.key_dir.mkdir(parents=True, exist_ok=True)
        key_file = registration.key_dir / f"{registration.slug}.private-key.pem"
        key_file.write_text(registration.pem, encoding="utf-8")
        key_file.chmod(0o600)
        return key_file

    async def _resolve_bot_email(self, slug: str) -> str:
        # The bot user usually exists right after the app is created; if the
        # lookup fails the field stays empty and the GUI's ordinary resolve
        # action can fill it in later.
        try:
            reference = await asyncio.to_thread(
                SimplePersonSetupService().resolve_github_user,
                slug,
                is_github_apps=True,
            )
        except SetupServiceError:
            return ""
        return reference.git_email

    def _purge_expired(self) -> None:
        deadline = time.time() - REGISTRATION_TTL_SECONDS
        for state, registration in list(self._registrations.items()):
            if registration.created_at < deadline:
                self._discard_unclaimed_key_file(registration)
                del self._registrations[state]

    @staticmethod
    def _discard_unclaimed_key_file(registration: GitHubAppRegistration) -> None:
        """Delete the written PEM for an abandoned registration.

        Member save absorbs the PEM into the OS secret store and deletes a
        flow-generated file. An expired registration that was never saved is
        an orphan and is cleaned up here.
        """
        if not registration.private_key_path:
            return
        Path(registration.private_key_path).unlink(missing_ok=True)
