import json
from pathlib import Path

import httpx
import pytest

from guildbotics.editions.simple import github_app_setup
from guildbotics.editions.simple.github_app_setup import (
    GitHubAppRegistrationService,
    STATUS_CONVERTED,
    STATUS_INSTALLED,
    STATUS_PENDING,
)
from guildbotics.editions.simple.setup_service import (
    GitHubUserReference,
    SetupServiceError,
    SimplePersonSetupService,
)
from guildbotics.integrations.github.app_manifest import (
    AppInstallation,
    AppManifestConversion,
)

CALLBACK_URL = "http://127.0.0.1:8765/github-app/registrations/callback"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nkey\n"


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    svc = GitHubAppRegistrationService()

    async def fake_convert(code: str, *, transport=None) -> AppManifestConversion:
        assert code == "tmp-code"
        return AppManifestConversion(
            app_id=1978826,
            slug="my-bot",
            html_url="https://github.com/apps/my-bot",
            pem=PEM,
        )

    async def fake_list(app_id: str, pem: bytes, *, transport=None):
        assert app_id == "1978826"
        return [
            AppInstallation(installation_id=11, account_login="a"),
            AppInstallation(installation_id=86632391, account_login="b"),
        ]

    monkeypatch.setattr(
        github_app_setup.app_manifest, "convert_manifest_code", fake_convert
    )
    monkeypatch.setattr(
        github_app_setup.app_manifest, "list_app_installations", fake_list
    )
    monkeypatch.setattr(
        SimplePersonSetupService,
        "resolve_github_user",
        lambda self, name, *, is_github_apps=False: GitHubUserReference(
            person_id=name,
            github_username=f"{name}[bot]",
            github_user_id=233270845,
            git_email=f"233270845+{name}[bot]@users.noreply.github.com",
        ),
    )
    return svc


def _start(service: GitHubAppRegistrationService, tmp_path: Path):
    return service.start(
        app_name="my-bot",
        person_id="alice",
        organization="",
        callback_url=CALLBACK_URL,
    )


async def _install(service: GitHubAppRegistrationService, tmp_path: Path):
    registration = _start(service, tmp_path)
    await service.complete(registration.state, "tmp-code")
    return await service.check_installation(registration.state)


def test_start_rejects_invalid_app_name(tmp_path: Path) -> None:
    service = GitHubAppRegistrationService()
    for name in ("", "  ", "x" * 35):
        with pytest.raises(SetupServiceError) as exc_info:
            service.start(
                app_name=name,
                person_id="alice",
                organization="",
                callback_url=CALLBACK_URL,
            )
        assert exc_info.value.code == "invalid_github_app_name"


def test_get_unknown_state_raises(tmp_path: Path) -> None:
    service = GitHubAppRegistrationService()
    with pytest.raises(SetupServiceError) as exc_info:
        service.get("missing")
    assert exc_info.value.code == "github_app_registration_not_found"


def test_expired_registration_is_purged(tmp_path: Path) -> None:
    service = GitHubAppRegistrationService()
    registration = _start(service, tmp_path)
    registration.created_at -= github_app_setup.REGISTRATION_TTL_SECONDS + 1
    with pytest.raises(SetupServiceError):
        service.get(registration.state)


def test_manifest_form_embeds_state_and_callback(tmp_path: Path) -> None:
    service = GitHubAppRegistrationService()
    registration = service.start(
        app_name="my-bot",
        person_id="alice",
        organization="acme",
        callback_url=CALLBACK_URL,
    )
    url, manifest_json = service.manifest_form(registration.state)
    assert url == (
        "https://github.com/organizations/acme/settings/apps/new"
        f"?state={registration.state}"
    )
    manifest = json.loads(manifest_json)
    assert manifest["name"] == "my-bot"
    assert manifest["redirect_url"] == CALLBACK_URL


@pytest.mark.asyncio
async def test_complete_holds_credentials_in_memory_only(
    service: GitHubAppRegistrationService, tmp_path: Path
) -> None:
    registration = _start(service, tmp_path)
    completed = await service.complete(registration.state, "tmp-code")

    assert completed.status == STATUS_CONVERTED
    assert completed.app_id == 1978826
    assert completed.slug == "my-bot"
    assert completed.github_username == "my-bot[bot]"
    assert completed.git_email == "233270845+my-bot[bot]@users.noreply.github.com"
    assert (
        completed.installation_page_url
        == "https://github.com/apps/my-bot/installations/new"
    )
    assert completed.pem == PEM
    assert "pem" not in completed.info_dump()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_complete_is_idempotent_after_conversion(
    service: GitHubAppRegistrationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _start(service, tmp_path)
    await service.complete(registration.state, "tmp-code")

    async def fail_convert(code: str, *, transport=None):
        raise AssertionError("conversion must not run twice")

    monkeypatch.setattr(
        github_app_setup.app_manifest, "convert_manifest_code", fail_convert
    )
    completed = await service.complete(registration.state, "tmp-code")
    assert completed.status == STATUS_CONVERTED


@pytest.mark.asyncio
async def test_complete_keeps_username_when_bot_lookup_fails(
    service: GitHubAppRegistrationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_resolve(self, name, *, is_github_apps=False):
        raise SetupServiceError("invalid_github_username", "Invalid GitHub username.")

    monkeypatch.setattr(
        SimplePersonSetupService, "resolve_github_user", failing_resolve
    )
    registration = _start(service, tmp_path)
    completed = await service.complete(registration.state, "tmp-code")
    assert completed.github_username == "my-bot[bot]"
    assert completed.git_email == ""


@pytest.mark.asyncio
async def test_check_installation_detects_latest_installation(
    service: GitHubAppRegistrationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = await _install(service, tmp_path)
    assert checked.status == STATUS_INSTALLED
    assert checked.installation_id == 86632391


@pytest.mark.asyncio
async def test_check_installation_is_noop_before_conversion(
    tmp_path: Path,
) -> None:
    service = GitHubAppRegistrationService()
    registration = _start(service, tmp_path)
    checked = await service.check_installation(registration.state)
    assert checked.status == STATUS_PENDING
    assert checked.installation_id is None


@pytest.mark.asyncio
async def test_claim_returns_the_installed_registration_until_discarded(
    service: GitHubAppRegistrationService, tmp_path: Path
) -> None:
    installed = await _install(service, tmp_path)

    # A save that fails after the claim retries with the same registration.
    assert service.claim(installed.state, "alice").pem == PEM
    assert service.claim(installed.state, "alice").installation_id == 86632391

    service.discard(installed.state)
    with pytest.raises(SetupServiceError) as exc_info:
        service.claim(installed.state, "alice")
    assert exc_info.value.code == "github_app_registration_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [0, 1])
async def test_claim_refuses_a_registration_not_installed_yet(
    service: GitHubAppRegistrationService, tmp_path: Path, steps: int
) -> None:
    registration = _start(service, tmp_path)
    if steps:
        await service.complete(registration.state, "tmp-code")

    with pytest.raises(SetupServiceError) as exc_info:
        service.claim(registration.state, "alice")
    assert exc_info.value.code == "github_app_registration_incomplete"


@pytest.mark.asyncio
async def test_claim_refuses_another_member(
    service: GitHubAppRegistrationService, tmp_path: Path
) -> None:
    installed = await _install(service, tmp_path)

    with pytest.raises(SetupServiceError) as exc_info:
        service.claim(installed.state, "bob")
    assert exc_info.value.code == "github_app_registration_member_mismatch"


@pytest.mark.asyncio
async def test_claim_refuses_an_expired_registration(
    service: GitHubAppRegistrationService, tmp_path: Path
) -> None:
    installed = await _install(service, tmp_path)
    installed.created_at -= github_app_setup.REGISTRATION_TTL_SECONDS + 1

    with pytest.raises(SetupServiceError) as exc_info:
        service.claim(installed.state, "alice")
    assert exc_info.value.code == "github_app_registration_not_found"


@pytest.mark.asyncio
async def test_check_installation_surfaces_transient_errors(
    service: GitHubAppRegistrationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _start(service, tmp_path)
    await service.complete(registration.state, "tmp-code")

    async def failing_list(app_id: str, pem: bytes, *, transport=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        github_app_setup.app_manifest, "list_app_installations", failing_list
    )
    checked = await service.check_installation(registration.state)
    assert checked.status == STATUS_CONVERTED
    assert "boom" in checked.installation_check_error
