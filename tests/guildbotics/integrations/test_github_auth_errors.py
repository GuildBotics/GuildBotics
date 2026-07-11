from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from guildbotics.integrations.github import async_client
from guildbotics.integrations.github import github_utils


def _github_app_auth(monkeypatch: pytest.MonkeyPatch) -> github_utils.GitHubAppAuth:
    auth = object.__new__(github_utils.GitHubAppAuth)
    auth.app_id = "1"
    auth.installation_id = "2"
    auth.person_id = "alice"
    auth._token = "expired"
    auth._expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    auth._leeway = dt.timedelta(seconds=120)
    monkeypatch.setattr(
        auth,
        "_build_refresh_request",
        lambda request: httpx.Request(
            "POST",
            request.url.copy_with(path="/app/installations/2/access_tokens"),
        ),
    )
    return auth


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
async def test_recoverable_github_app_unauthorized_is_left_to_auth_flow() -> None:
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    response = httpx.Response(401, request=request, text="expired")

    returned = await async_client.raise_for_status_with_text(
        response, handle_unauthorized=False
    )

    assert returned is response


def test_github_app_refresh_success_does_not_record_failure(monkeypatch) -> None:
    auth = _github_app_auth(monkeypatch)
    recorded: list[dict[str, str]] = []
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    flow = auth.auth_flow(request)

    first_request = next(flow)
    refresh_request = flow.send(httpx.Response(401, request=first_request))
    retry_request = flow.send(
        httpx.Response(
            201,
            request=refresh_request,
            json={"token": "fresh", "expires_at": "2026-07-12T00:00:00Z"},
        )
    )
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200, request=retry_request))

    assert retry_request.headers["Authorization"] == "token fresh"
    assert recorded == []


def test_github_app_retry_unauthorized_records_failure_once(monkeypatch) -> None:
    auth = _github_app_auth(monkeypatch)
    recorded: list[dict[str, str]] = []
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    flow = auth.auth_flow(request)

    first_request = next(flow)
    refresh_request = flow.send(httpx.Response(401, request=first_request))
    retry_request = flow.send(
        httpx.Response(
            201,
            request=refresh_request,
            json={"token": "fresh", "expires_at": "2026-07-12T00:00:00Z"},
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        flow.send(httpx.Response(401, request=retry_request))

    assert recorded == [{"person_id": "alice"}]


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
