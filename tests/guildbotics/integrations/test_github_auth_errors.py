from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from guildbotics.integrations.github import async_client
from guildbotics.integrations.github import github_utils


@pytest.mark.asyncio
async def test_unauthorized_response_records_credential_failure(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(
        async_client,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    response = httpx.Response(401, request=request, text="unauthorized")

    with pytest.raises(httpx.HTTPStatusError):
        await async_client.raise_for_status_with_text(response)

    assert recorded[0]["event_type"] == "credential.failed"
    assert recorded[0]["payload"] == {
        "provider": "github",
        "code": "unauthorized",
    }


@pytest.mark.asyncio
async def test_invalid_github_app_key_records_credential_failure(monkeypatch) -> None:
    recorded = []
    person = SimpleNamespace(
        person_id="alice",
        get_secret=lambda key: {
            "github_app_id": "1",
            "github_installation_id": "2",
        }.get(key, ""),
    )
    monkeypatch.setattr(
        github_utils,
        "get_github_account_type",
        lambda _person: github_utils.GitHubAppAuth.GITHUB_APPS,
    )
    monkeypatch.setattr(
        github_utils, "get_person_private_key_pem", lambda _person: b"invalid"
    )
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )

    with pytest.raises(ValueError):
        await github_utils.create_github_client(person, "https://api.github.com")

    assert recorded == [{"person_id": "alice", "code": "invalid_app_credential"}]
