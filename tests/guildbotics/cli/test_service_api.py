"""Errors and language at the common service API boundary."""

from contextlib import contextmanager

import httpx
import pytest
from click.testing import CliRunner

from guildbotics.cli import main, service
from guildbotics.drivers.app_api_lock import BackendMetadata


@pytest.fixture
def api(monkeypatch):
    settings = {
        "status": 409,
        "body": {"message": "The server is shutting down."},
    }
    connections = []
    requests = []

    def respond(request):
        requests.append(request)
        if "failure" in settings:
            raise settings["failure"]
        if "content" in settings:
            return httpx.Response(settings["status"], content=settings["content"])
        return httpx.Response(settings["status"], json=settings["body"])

    @contextmanager
    def connect(**kwargs):
        connections.append(kwargs)
        with httpx.Client(
            base_url="http://127.0.0.1:8765/api",
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(respond),
        ) as client:
            yield client

    monkeypatch.setattr(service, "connect_local_api", connect)
    return settings, connections, requests


@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize("status_code", [409, 503])
def test_service_api_preserves_error_reason_and_requested_language(
    monkeypatch, api, language, status_code
):
    settings, connections, requests = api
    message = (
        "サーバーは終了中です。" if language == "ja" else settings["body"]["message"]
    )
    settings["body"] = {"code": "server_closing", "message": message}
    settings["status"] = status_code
    monkeypatch.setattr(service, "get_language", lambda: language, raising=False)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        service._call("/shutdown", payload={"force": False}, timeout=3)
    assert str(caught.value) == message
    assert caught.value.response.status_code == status_code
    assert connections == [{"headers": {"Accept-Language": language}, "timeout": 3}]
    assert len(requests) == 1
    assert requests[0].url.path == "/api/shutdown"
    assert requests[0].method == "POST"
    assert requests[0].headers["Accept-Language"] == language


@pytest.mark.parametrize("body", [{}, [], {"message": None}, {"message": ""}])
def test_malformed_api_error_preserves_the_http_failure(api, body):
    settings, _, requests = api
    settings["body"] = body
    with pytest.raises(httpx.HTTPStatusError):
        service._call("/shutdown", payload={"force": False}, timeout=3)
    assert len(requests) == 1


def test_non_json_error_preserves_the_http_failure(api):
    settings, _, requests = api
    settings["content"] = "temporarily unavailable"
    with pytest.raises(httpx.HTTPStatusError):
        service._call("/shutdown", payload={"force": False}, timeout=3)
    assert len(requests) == 1


@pytest.mark.parametrize("command", ["stop", "kill"])
@pytest.mark.parametrize("connection_lost", [False, True])
def test_stop_commands_show_the_api_reason_without_retry(
    monkeypatch, api, command, connection_lost
):
    settings, _, requests = api
    if connection_lost:
        settings["failure"] = httpx.ReadError("reply lost")
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)
    monkeypatch.setattr(
        service, "inspect_backend_lock", lambda: (True, BackendMetadata(42))
    )
    result = CliRunner().invoke(main, [command])
    assert result.exit_code == 1
    assert (
        "reply lost" if connection_lost else settings["body"]["message"]
    ) in result.output
    assert len(requests) == 1
