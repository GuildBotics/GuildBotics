"""GitHub App auto-registration flow used by the desktop member setup GUI.

The flow is a semi-automatic browser round trip: the GUI starts a
registration, the user's browser posts the app manifest to github.com and
clicks "Create GitHub App", GitHub redirects back to the local API with a
one-time code, and this module converts the code into credentials. The GUI
then polls the registration until the user has installed the app and the
installation ID could be detected.

Registrations are held in memory only. The PEM never leaves this process
except into the OS secret store: the member save names the registration, takes
the key and the app's IDs from it here, and discards it once the member and
the key are saved.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

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
    person_id: str
    slug: str = ""
    app_id: int | None = None
    html_url: str = ""
    github_username: str = ""
    git_email: str = ""
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
        person_id: str,
        organization: str,
        callback_url: str,
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
            person_id=person_id,
            organization=organization.strip(),
            callback_url=callback_url,
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

    def claim(self, state: str, person_id: str) -> GitHubAppRegistration:
        """Return the installed registration a save of ``person_id`` names.

        It stays held, so a save that fails after this can be retried with
        the same registration; :meth:`discard` it once the save succeeded.

        Raises:
            SetupServiceError: The registration is missing, expired or already
                saved, is not installed yet, or was started for another member.
        """
        registration = self.get(state)
        if registration.status != STATUS_INSTALLED:
            raise SetupServiceError(
                "github_app_registration_incomplete",
                "GitHub App registration has not been installed yet.",
            )
        if registration.person_id != person_id:
            raise SetupServiceError(
                "github_app_registration_member_mismatch",
                "GitHub App registration was started for another member.",
            )
        return registration

    def discard(self, state: str) -> None:
        """Forget a registration whose key the secret store now holds."""
        self._registrations.pop(state, None)

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
        self._registrations = {
            state: registration
            for state, registration in self._registrations.items()
            if registration.created_at >= deadline
        }
