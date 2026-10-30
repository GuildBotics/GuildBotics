"""Who may use the server: the owner token file, entry links into a browser
session, local apps through the consent screen, and the guard before all of
them."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guildbotics.app_api import auth
from guildbotics.app_api.api import create_app
from guildbotics.app_api.auth import CSRF_HEADER, ServerAuth
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.utils.local_api import owner_token_path
from tests.guildbotics.app_api.owner import (
    EVENTS_URL,
    ORIGIN,
    browser_client,
    owner_client,
    owner_token,
)

VERIFIER = "v" * 50
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)
REDIRECT = "http://127.0.0.1:5555/callback"
CONSENT = {
    "client": "GuildBotics CLI laptop",
    "redirect_uri": REDIRECT,
    "state": "s",
    "code_challenge": CHALLENGE,
}


@pytest.fixture
def app(workspace) -> FastAPI:
    return create_app(runtime=AppRuntime(EventBus()))


def _anonymous(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN + "/api")


def _entry_code(app: FastAPI, **body: str) -> str:
    link = owner_client(app).post("/session/links", json=body or None).json()["url"]
    return link.split("#", 1)[1]


def _consented(app: FastAPI, **changes: str) -> dict[str, str]:
    answer = owner_client(app).post("/apps/authorizations", json=CONSENT | changes)
    assert answer.status_code == 201, answer.text
    query = parse_qs(urlsplit(answer.json()["redirect"]).query)
    return {key: values[0] for key, values in query.items()}


# -- the guard ------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/health", "/", "/-/enter", "/quick.html"])
@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:1420", "127.0.0.1"])
def test_a_request_for_another_host_is_misdirected(app, path, host):
    answer = TestClient(app, base_url=f"http://{host}").get(
        path, headers={"Authorization": f"Bearer {owner_token(app)}"}
    )
    assert answer.status_code == 421
    assert answer.json()["code"] == "misdirected"
    assert answer.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_the_loopback_names_of_the_port_are_this_server(app, host):
    answer = owner_client(app).get("/health", headers={"Host": host})
    assert answer.status_code == 200


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "null", "http://localhost:8765", "tauri://localhost"],
)
def test_a_request_from_another_origin_is_refused_before_it_runs(app, origin):
    answer = owner_client(app).post("/session/links", headers={"Origin": origin})
    assert answer.status_code == 403
    assert answer.json()["code"] == "origin_mismatch"


def test_every_response_carries_the_security_headers_and_no_cors(app):
    for answer in (
        owner_client(app).get("/health", headers={"Origin": ORIGIN}),
        _anonymous(app).get("/health"),
        TestClient(app, base_url=ORIGIN).get("/"),
    ):
        csp = answer.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp
        assert answer.headers["x-frame-options"] == "DENY"
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["referrer-policy"] == "no-referrer"
        assert not any(key.startswith("access-control-") for key in answer.headers)


# -- credentials ----------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer", "Bearer gbt_0123456789abcdef_wrong", "Basic abc", "bearer x"],
)
def test_without_a_known_token_or_session_nothing_is_served(app, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    answer = _anonymous(app).get("/health", headers=headers)
    assert answer.status_code == 401
    assert answer.json()["code"] == "unauthorized"


def test_a_session_writes_only_with_its_own_csrf_value(app):
    client = browser_client(app)
    other = browser_client(app)
    assert client.get("/config/status").status_code == 200
    assert client.post("/session/links").status_code == 201
    for csrf in ("", "wrong", other.headers[CSRF_HEADER]):
        answer = client.post("/session/links", headers={CSRF_HEADER: csrf})
        assert answer.status_code == 403
        assert answer.json()["code"] == "csrf_mismatch"
    assert client.get("/session").json()["csrf"] == client.headers[CSRF_HEADER]


def test_an_entry_code_works_once_and_sets_a_strict_session_cookie(app):
    code = _entry_code(app)
    first = _anonymous(app).post("/session", json={"code": code})
    assert first.status_code == 200
    cookie = first.headers["set-cookie"]
    assert cookie.startswith("guildbotics_session_8765=")
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert attribute in cookie
    assert _anonymous(app).post("/session", json={"code": code}).status_code == 401
    assert _anonymous(app).post("/session", json={"code": "x"}).status_code == 401


def test_an_entry_code_expires(app, monkeypatch):
    code = _entry_code(app)
    later = auth._now() + auth.CODE_TTL_SECONDS + 1
    monkeypatch.setattr(auth, "_now", lambda: later)
    assert _anonymous(app).post("/session", json={"code": code}).status_code == 401


def test_an_entry_link_names_the_page_to_land_on_and_keeps_the_code_in_its_fragment(
    app,
):
    plain = owner_client(app).post("/session/links").json()["url"]
    assert urlsplit(plain)._replace(fragment="").geturl() == f"{ORIGIN}/-/enter"
    quick = owner_client(app).post("/session/links", json={"next": "/quick.html"})
    parts = urlsplit(quick.json()["url"])
    assert parts.path == "/-/enter"
    assert parse_qs(parts.query) == {"next": ["/quick.html"]}
    assert parts.fragment


@pytest.mark.parametrize(
    "next_path", ["//evil.example", "/\\evil", "https://x", "a", "/ a"]
)
def test_an_entry_link_lands_only_on_this_server(app, next_path):
    answer = owner_client(app).post("/session/links", json={"next": next_path})
    assert answer.status_code == 422


def test_a_session_outlives_the_server_process(app, workspace):
    client = browser_client(app)
    restarted = create_app(runtime=AppRuntime(EventBus()))
    again = TestClient(restarted, base_url=ORIGIN + "/api", cookies=client.cookies)
    assert again.get("/session").json()["csrf"] == client.headers[CSRF_HEADER]


def test_a_new_owner_token_ends_every_session(app):
    client = browser_client(app)
    previous = owner_token(app)
    owner_token_path(8765).unlink()
    restarted = create_app(runtime=AppRuntime(EventBus()))
    assert owner_token(restarted) != previous
    again = TestClient(restarted, base_url=ORIGIN + "/api", cookies=client.cookies)
    assert again.get("/session").status_code == 401
    old = _anonymous(restarted).get(
        "/health", headers={"Authorization": f"Bearer {previous}"}
    )
    assert old.status_code == 401


def test_an_owner_file_that_still_authenticates_is_kept():
    first = ServerAuth(8765)
    token = owner_token_path(8765).read_text("utf-8")
    ServerAuth(8765)
    assert owner_token_path(8765).read_text("utf-8") == token
    assert first.port == 8765


def test_the_server_keeps_only_digests(app):
    browser_client(app)
    records = (owner_token_path(8765).parent / "auth.json").read_text("utf-8")
    token = owner_token(app)
    assert token not in records and token.rsplit("_", 1)[1] not in records


def test_the_websocket_needs_a_session_and_this_origin(app):
    client = browser_client(app)
    with client.websocket_connect(EVENTS_URL) as socket:
        assert socket is not None
    for headers in ({}, {"Origin": "http://127.0.0.1:1420"}):
        refused = TestClient(app, cookies=client.cookies)
        with (
            pytest.raises(Exception),
            refused.websocket_connect(  # noqa: B017
                EVENTS_URL, headers=headers
            ),
        ):
            pass
    with (
        pytest.raises(Exception),
        _anonymous(app).websocket_connect(  # noqa: B017
            EVENTS_URL, headers={"Origin": ORIGIN}
        ),
    ):
        pass


# -- local apps -----------------------------------------------------------------


def test_a_consented_app_gets_an_owner_token_of_its_own(app):
    query = _consented(app)
    assert query["state"] == "s"
    answer = _anonymous(app).post(
        "/apps/token",
        json={
            "code": query["code"],
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
    )
    assert answer.status_code == 201
    issued = answer.json()
    assert issued["label"] == "app:GuildBotics CLI laptop"
    with_token = {"Authorization": f"Bearer {issued['token']}"}
    assert _anonymous(app).get("/health", headers=with_token).status_code == 200


def test_a_second_consent_of_the_same_app_revokes_its_previous_token(app):
    tokens = []
    for _ in range(2):
        code = _consented(app)["code"]
        tokens.append(
            _anonymous(app)
            .post(
                "/apps/token",
                json={
                    "code": code,
                    "redirect_uri": REDIRECT,
                    "code_verifier": VERIFIER,
                },
            )
            .json()["token"]
        )
    first, second = ({"Authorization": f"Bearer {token}"} for token in tokens)
    assert _anonymous(app).get("/health", headers=first).status_code == 401
    assert _anonymous(app).get("/health", headers=second).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"redirect_uri": "http://127.0.0.1:5556/callback"},
        {"code_verifier": "w" * 50},
        {"code_verifier": "short"},
    ],
)
def test_an_exchange_that_does_not_match_its_code_is_refused_and_spends_it(app, change):
    code = _consented(app)["code"]
    body = {"code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER}
    refused = _anonymous(app).post("/apps/token", json=body | change)
    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid"
    assert _anonymous(app).post("/apps/token", json=body).status_code == 400


def test_a_consent_code_expires(app, monkeypatch):
    code = _consented(app)["code"]
    later = auth._now() + auth.CODE_TTL_SECONDS + 1
    monkeypatch.setattr(auth, "_now", lambda: later)
    answer = _anonymous(app).post(
        "/apps/token",
        json={"code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER},
    )
    assert answer.status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"client": ""},
        {"client": "   "},
        {"client": "a" * 65},
        {"client": "bad/name"},
        {"redirect_uri": "https://127.0.0.1/callback"},
        {"redirect_uri": "http://evil.example/callback"},
        {"redirect_uri": "http://user@127.0.0.1/callback"},
        {"redirect_uri": "http://127.0.0.1/callback#x"},
        {"redirect_uri": "http://127.0.0.1:99999/callback"},
        {"state": ""},
        {"state": "s" * 513},
        {"code_challenge": "short"},
        {"code_challenge": CHALLENGE[:-1] + "="},
    ],
)
def test_a_consent_outside_the_rules_is_refused(app, change):
    answer = owner_client(app).post("/apps/authorizations", json=CONSENT | change)
    assert answer.status_code == 400
    assert answer.json()["code"] == "invalid"


@pytest.mark.parametrize(
    "redirect",
    ["http://127.0.0.1/cb", "http://localhost:9/cb?x=1", "http://[::1]:7/cb"],
)
def test_a_consent_keeps_the_redirects_own_query(app, redirect):
    query = _consented(app, redirect_uri=redirect)
    assert set(query) >= {"code", "state"}
    if "?x=1" in redirect:
        assert query["x"] == "1"


def test_a_consent_needs_the_owner(app):
    answer = _anonymous(app).post("/apps/authorizations", json=CONSENT)
    assert answer.status_code == 401


def test_a_session_may_consent_with_its_csrf_value(app):
    client = browser_client(app)
    assert client.post("/apps/authorizations", json=CONSENT).status_code == 201
    del client.headers[CSRF_HEADER]
    assert client.post("/apps/authorizations", json=CONSENT).status_code == 403


def test_a_session_in_use_is_renewed_and_an_idle_one_expires(app, monkeypatch):
    used = browser_client(app)
    idle = browser_client(app)
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + auth.SESSION_SECONDS * 0.6)
    assert used.get("/config/status").status_code == 200
    monkeypatch.setattr(auth, "_now", lambda: start + auth.SESSION_SECONDS * 1.1)
    assert used.get("/config/status").status_code == 200
    assert idle.get("/config/status").status_code == 401


def test_each_port_has_its_own_session_cookie(workspace):
    assert ServerAuth(8765).cookie != ServerAuth(8766).cookie
    assert not ServerAuth(8765).cookie.startswith("branvil_session")
