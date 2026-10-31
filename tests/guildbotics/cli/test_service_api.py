"""Errors and language at the common service API boundary."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from guildbotics.cli import main, service
from guildbotics.drivers.app_api_lock import BackendMetadata


@pytest.fixture
def api(monkeypatch):
    settings = {
        "status": 409,
        "body": {"message": "Service owner conflict: transfer ownership first."},
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


@pytest.mark.parametrize(
    "path,method",
    [
        ("/scheduler/start", "POST"),
        ("/scheduler/stop", "POST"),
        ("/scheduler/status", "GET"),
        ("/shutdown", "POST"),
    ],
)
@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize("status_code", [409, 503])
def test_service_api_preserves_error_reason_and_requested_language(
    monkeypatch, api, path, method, language, status_code
):
    settings, connections, requests = api
    message = (
        "サービスオーナーが異なります。明示的に引き継いでください。"
        if language == "ja"
        else settings["body"]["message"]
    )
    settings["body"] = {"code": "service_owner_conflict", "message": message}
    settings["status"] = status_code
    monkeypatch.setattr(service, "get_language", lambda: language, raising=False)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        service._call(path, method=method, timeout=3)
    assert str(caught.value) == message
    assert caught.value.response.status_code == status_code
    assert connections == [{"headers": {"Accept-Language": language}, "timeout": 3}]
    assert len(requests) == 1
    assert requests[0].url.path == "/api" + path
    assert requests[0].method == method
    assert requests[0].headers["Accept-Language"] == language


@pytest.mark.parametrize("body", [{}, [], {"message": None}, {"message": ""}])
def test_malformed_api_error_preserves_the_http_failure(api, body):
    settings, _, requests = api
    settings["body"] = body
    with pytest.raises(httpx.HTTPStatusError):
        service._call("/scheduler/start")
    assert len(requests) == 1


def test_non_json_error_preserves_the_http_failure(api):
    settings, _, requests = api
    settings["content"] = "temporarily unavailable"
    with pytest.raises(httpx.HTTPStatusError):
        service._call("/scheduler/start")
    assert len(requests) == 1


def test_success_is_unchanged(api):
    settings, _, requests = api
    settings.update(status=200, body={"scheduler": {"running": True}})
    assert service._call("/scheduler/start") == settings["body"]
    assert len(requests) == 1


@pytest.mark.parametrize("command", ["start", "stop", "kill"])
@pytest.mark.parametrize("connection_lost", [False, True])
def test_public_commands_show_the_api_reason_without_retry(
    monkeypatch, tmp_path, api, command, connection_lost
):
    settings, _, requests = api
    if connection_lost:
        settings["failure"] = httpx.ReadError("reply lost")
    closed = []
    monkeypatch.setattr(service, "selected_workspace", lambda: tmp_path)
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)
    if command == "start":
        monkeypatch.setattr(service, "inspect_backend_lock", lambda: (False, None))
        monkeypatch.setattr(
            service,
            "_launch_backend",
            lambda workspace: (
                SimpleNamespace(poll=lambda: None),
                None,
            ),
        )
        monkeypatch.setattr(service, "_wait_for_port", lambda *args: None)
        monkeypatch.setattr(
            service, "_close_backend", lambda *args, **kwargs: closed.append(True)
        )
    else:
        monkeypatch.setattr(
            service, "inspect_backend_lock", lambda: (True, BackendMetadata("cli", 42))
        )
    result = CliRunner().invoke(main, [command])
    assert result.exit_code == 1
    assert (
        "reply lost" if connection_lost else settings["body"]["message"]
    ) in result.output
    assert len(requests) == 1
    assert closed == ([True] if command == "start" else [])


def test_shutdown_rejection_is_shown_and_still_reaps_the_child(
    monkeypatch, api, capsys
):
    settings, _, requests = api
    calls = []

    def wait(timeout=None):
        calls.append(("wait", timeout))
        if timeout is not None:
            raise service.subprocess.TimeoutExpired("backend", timeout)

    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: calls.append("terminate"),
        wait=wait,
        kill=lambda: calls.append("kill"),
        stdout=SimpleNamespace(close=lambda: calls.append("close")),
    )
    service._close_backend(process, announced=True)
    assert settings["body"]["message"] in capsys.readouterr().err
    assert calls == ["terminate", ("wait", 5), "kill", ("wait", None), "close"]
    assert len(requests) == 1
