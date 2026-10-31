"""``guildbotics open`` and ``guildbotics connect`` against the real server
routes, in process."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.cli import server
from guildbotics.utils import local_api
from guildbotics.utils.local_api import client_tokens, owner_token_path
from tests.guildbotics.app_api.owner import ORIGIN, browser_client


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The server, which ``httpx`` reaches in process on its loopback port."""
    monkeypatch.chdir(tmp_path)
    app = create_app(runtime=AppRuntime(EventBus()))
    test_client = TestClient(app, base_url=ORIGIN)

    def exchange(request: httpx.Request) -> httpx.Response:
        if request.url.host != "127.0.0.1" or request.url.port != 8765:  # noqa: PLR2004
            raise httpx.ConnectError("refused", request=request)
        answer = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(answer.status_code, content=answer.content)

    transport = httpx.MockTransport(exchange)
    client_type = httpx.Client
    monkeypatch.setattr(
        local_api.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=transport),
    )
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, **kwargs: client_type(transport=transport).post(
            url, **{k: v for k, v in kwargs.items() if k != "trust_env"}
        ),
    )
    return app


def test_open_prints_a_single_use_entry_link(app):
    result = CliRunner().invoke(server.open_screens, ["--print"])
    assert result.exit_code == 0, result.output
    url = result.output.strip()
    assert url.startswith(f"{ORIGIN}/-/enter#")
    anonymous = TestClient(app, base_url=ORIGIN + "/api")
    code = url.split("#", 1)[1]
    assert anonymous.post("/session", json={"code": code}).status_code == 200


def test_open_opens_the_browser(app, monkeypatch):
    opened = []
    monkeypatch.setattr(
        server.webbrowser, "open", lambda url: opened.append(url) or True
    )
    result = CliRunner().invoke(server.open_screens, [])
    assert result.exit_code == 0 and result.output == ""
    assert opened[0].startswith(f"{ORIGIN}/-/enter#")


def test_open_without_a_credential_says_how_to_get_one(app):
    owner_token_path(8765).unlink()
    result = CliRunner().invoke(server.open_screens, ["--print"])
    assert result.exit_code != 0
    assert "guildbotics connect" in result.output


def _consent(app, answer: str):
    """The owner's browser on the consent screen: Allow or Deny. The browser
    entered before this machine lost the owner file, as another machine's
    browser would have."""
    browser = browser_client(app)
    away = owner_token_path(8765).with_suffix(".away")
    owner_token_path(8765).rename(away)

    def opened(url: str) -> bool:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert urlsplit(url).path == "/-/authorize"
        assert query["code_challenge_method"] == "S256"
        if answer == "allow":
            redirect = browser.post(
                "/apps/authorizations",
                json={
                    key: query[key]
                    for key in ("client", "redirect_uri", "state", "code_challenge")
                },
            ).json()["redirect"]
        else:
            redirect = (
                f"{query['redirect_uri']}?error=access_denied&state={query['state']}"
            )
        # Another local request knows no state, and does not end the wait.
        stray = httpx.get(
            f"{query['redirect_uri']}?code=x&state=other", trust_env=False
        )
        assert stray.status_code == 404
        httpx.get(redirect, trust_env=False)
        return True

    return opened


def test_connect_keeps_the_token_the_consent_gives(app, monkeypatch):
    monkeypatch.setattr(server.webbrowser, "open", _consent(app, "allow"))
    result = CliRunner().invoke(server.connect, [])
    assert result.exit_code == 0, result.output
    [token] = client_tokens(8765, consent=True)
    assert token.startswith("gbt_")
    anonymous = TestClient(app, base_url=ORIGIN + "/api")
    answer = anonymous.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 200


def test_connect_keeps_nothing_when_the_owner_denies(app, monkeypatch):
    monkeypatch.setattr(server.webbrowser, "open", _consent(app, "deny"))
    result = CliRunner().invoke(server.connect, [])
    assert result.exit_code != 0
    assert list(client_tokens(8765, consent=True)) == []


def test_the_machine_name_fits_the_consent_rules(monkeypatch):
    monkeypatch.setattr(server.socket, "gethostname", lambda: "ゆき's PC/1." + "x" * 80)
    name = server._client_name()
    assert len(name) <= 64 and name.startswith("GuildBotics CLI ")
    assert all(c.isalnum() and c.isascii() or c in " ._-" for c in name)
