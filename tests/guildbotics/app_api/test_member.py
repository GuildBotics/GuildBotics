"""Member CLI and backend contracts, with no Desktop or external provider."""

import asyncio
import contextvars
import importlib
import io
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from guildbotics.app_api import member as transport
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.cli import _resolve_version, main, member_client
from guildbotics.cli.member import _HostPath, member
from guildbotics.cli.member_plan import plan
from guildbotics.environment.member_execution import MemberWorkers
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.workspace.identity import ensure_workspace_identity
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.app_api.owner import owner_client
from tests.guildbotics.local_code_host import attachment, local_member, ticket, tracker

backend = importlib.import_module("guildbotics.cli.member")


@pytest.fixture
def connected(tmp_path, monkeypatch):
    identity = ensure_workspace_identity()
    context = local_member()
    monkeypatch.setattr(backend, "_resolve", lambda person: (context, context.person))
    runtime = AppRuntime(EventBus())
    client = owner_client(create_app(runtime=runtime))

    @contextmanager
    def connect(**kwargs):
        assert "consent" not in kwargs
        yield client

    monkeypatch.setattr(member_client, "connect_local_api", connect)
    monkeypatch.chdir(tmp_path)
    yield client, runtime, identity.workspace_id
    client.close()


def request(workspace_id, arguments):
    return {
        "version": _resolve_version(),
        "workspace_id": workspace_id,
        "arguments": arguments,
    }


def decode(response):
    assert response.status_code == 200, response.text
    line, _, payload = response.content.partition(b"\n")
    return json.loads(line), payload


def test_entry_import_is_thin():
    probe = """
import sys
from guildbotics.cli import main
import click
main.get_command(click.Context(main), 'member')
assert 'guildbotics.cli.member' not in sys.modules
assert 'keyring' not in sys.modules
assert not any(n.startswith(('guildbotics.integrations.', 'guildbotics.drivers.context')) for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_path_option_population_is_classified():
    paths = {}

    def walk(command, names=()):
        for param in command.params:
            if isinstance(param.type, _HostPath):
                paths[(*names, param.name)] = param.type.client_kind
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, (*names, name))

    walk(member)
    expected = {
        ("git", name, "repo_path"): "git" for name in ("commit", "push", "publish")
    }
    expected.update(
        {
            ("pr", "artifact", "download", "dest"): "download",
            ("ticket", "attachment", "download", "dest"): "download",
        }
    )
    content = {key for key, kind in paths.items() if kind == "content"}
    assert content
    assert all(key[-1] == "content_file" for key in content)
    assert {
        key: value for key, value in paths.items() if key not in content
    } == expected


@pytest.mark.parametrize(
    "file_option", [["--content-file", "relative.txt"], ["--content-file=relative.txt"]]
)
def test_plan_rewrites_only_content_option(file_option):
    args = [
        "ticket",
        "create",
        "--person",
        "aiko",
        "--tracker",
        "demo",
        "--title",
        "--content-file",
        *file_option,
    ]
    described = plan(args)
    assert described["content_file"] == "relative.txt"
    assert described["arguments"] == args[:8] + ["--content-stdin"]


def test_cli_help_and_usage_come_from_backend(connected):
    for args in (["pr", "read", "--help"], ["not-a-command"]):
        expected = plan(list(args))["result"]
        result = CliRunner().invoke(main, ["member", *args])
        assert result.exit_code == expected["exit_code"], result.output
        assert " ".join(result.stdout.split()) == " ".join(expected["stdout"].split())
        assert result.stderr == expected["stderr"]


@pytest.mark.parametrize("use_file", [True, False])
def test_cli_transfers_content_and_preserves_backend_output(
    connected, tmp_path, use_file
):
    client, runtime, workspace_id = connected
    tracker()
    url = ticket()
    content = "調査結果\nUTF-8 の本文です。\n"
    path = tmp_path / "body.txt"
    path.write_text(content, encoding="utf-8")
    option = ["--content-file", str(path)] if use_file else ["--content-stdin"]
    result = CliRunner().invoke(
        main,
        ["member", "ticket", "comment", "--person", "aiko", "--url", url, *option],
        input=content,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["target"]["url"] == url
    context, _ = backend._resolve("aiko")
    comments = asyncio.run(context.tickets.read("ticket_comments", url))
    assert comments.items[0]["body"] == content.strip()
    assert not runtime._accepted_command_ids


@pytest.mark.parametrize("mode", ["json", "markdown"])
@pytest.mark.windows
def test_download_is_streamed_and_written_only_by_client(connected, tmp_path, mode):
    client, _, workspace_id = connected
    tracker()
    url = ticket(body=attachment("image-1", b"image-data", name="image.png"))
    args = [
        "ticket",
        "attachment",
        "download",
        "--person",
        "aiko",
        "--url",
        url,
        "--id",
        "image-1",
        "--dest",
        r"C:\client\images",
    ]
    metadata, payload = decode(
        client.post("/member/run", json=request(workspace_id, args))
    )
    assert metadata["exit_code"] == 0, metadata
    assert metadata["download"]["destination"] == r"C:\client\images"
    assert payload == b"image-data"
    assert not (tmp_path / r"C:\client\images").exists()
    args[-1] = str(tmp_path / "out")
    result = CliRunner().invoke(main, ["member", *args, "--format", mode])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out/image.png").read_bytes() == b"image-data"
    assert "image.png" in result.stdout


def test_memory_redacts_local_names_before_sending(connected, tmp_path, monkeypatch):
    # A non-pattern key proves that only reading the environment's names
    # would miss values whose provenance is the saved index.
    index = tmp_path / ".guildbotics/config/secrets.yml"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("keys:\n  DATABASE_URL: {}\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "local-dummy-value")
    seen = []
    plans = []
    original_plan = transport.plan

    def describe(arguments):
        plans.append(arguments)
        return original_plan(arguments)

    monkeypatch.setattr(transport, "plan", describe)

    def run(arguments, invocation, **kwargs):
        seen.append((arguments, kwargs["stdin"]))
        return 0, "{}\n", ""

    monkeypatch.setattr(transport, "run_in_process", run)
    result = CliRunner().invoke(
        main,
        [
            "member",
            "memory",
            "record",
            "--person",
            "aiko",
            "--title",
            "local-dummy-value",
            "--content-stdin",
        ],
        input="body local-dummy-value",
    )
    assert result.exit_code == 0, result.output
    assert seen[0][1] == "body ***"
    assert "local-dummy-value" not in str(seen)
    assert "local-dummy-value" not in str(plans)


@pytest.mark.parametrize("file_kind", ["missing", "invalid_utf8", "directory"])
def test_content_read_failures_stay_local(connected, tmp_path, file_kind):
    path = tmp_path / "body"
    if file_kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif file_kind == "directory":
        path.mkdir()
    result = CliRunner().invoke(
        main,
        [
            "member",
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "local://tickets/demo/T-1",
            "--content-file",
            str(path),
        ],
    )
    assert result.exit_code == 1
    key = "file_not_utf8" if file_kind == "invalid_utf8" else "file_read_failed"
    assert str(path) in result.stderr
    assert (
        t(f"cli.member.content.{key}", path=path, error="").split(str(path))[0]
        in result.stderr
    )


def test_conflicting_content_does_not_read_a_file(connected):
    result = CliRunner().invoke(
        main,
        [
            "member",
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "local://tickets/demo/T-1",
            "--content-file",
            "absent",
            "--content-stdin",
        ],
    )
    assert result.exit_code == 2
    assert t("cli.member.content.exclusive") in result.stderr


def test_help_requires_a_running_backend(connected, monkeypatch):
    @contextmanager
    def disconnected(**kwargs):
        yield None

    monkeypatch.setattr(member_client, "connect_local_api", disconnected)
    result = CliRunner().invoke(main, ["member", "--help"])
    assert result.exit_code == 1
    assert t("cli.member.client.backend_required") in result.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"version": "wrong"},
        {"workspace_id": "other"},
        {"run_id": "forged"},
        {"lease": {}},
        {"guest": {}},
        {"arguments": ["--workspace=elsewhere", "help"]},
    ],
)
def test_api_refuses_wrong_authority_before_execution(connected, monkeypatch, change):
    client, runtime, workspace_id = connected

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not execute")

    monkeypatch.setattr(transport, "run_in_process", forbidden)
    response = client.post(
        "/member/run", json={**request(workspace_id, ["help"]), **change}
    )
    assert response.status_code in {409, 422}
    assert not runtime._accepted_command_ids


def test_backend_rejects_current_and_content_paths(connected, tmp_path):
    client, _, workspace_id = connected
    path = tmp_path / "never-read.txt"
    path.write_text("local content", encoding="utf-8")
    base = ["git", "commit", "--person", "aiko", "--repo-path", str(tmp_path)]
    for args in (
        [*base, "--content-file", str(path)],
        [*base, "--content-stdin", "--workspace-mode", "current"],
    ):
        result, _ = decode(
            client.post(
                "/member/run", json={**request(workspace_id, args), "stdin": "message"}
            )
        )
        assert result["exit_code"] != 0


def test_health_advertises_version_and_identity(connected):
    client, _, workspace_id = connected
    health = client.get("/health").json()
    assert health["version"] == _resolve_version()
    assert health["workspace_id"] == workspace_id


@pytest.mark.windows
def test_current_publication_crosses_api_with_bundles(connected, tmp_path, monkeypatch):
    from guildbotics.integrations.local import store
    from guildbotics.local_git import LocalGit
    from tests.guildbotics.local_code_host import REPOSITORY, remote_with

    remote_with()
    user = tmp_path / "user"
    subprocess.run(
        ["git", "clone", "-q", store.bare(REPOSITORY).as_uri(), str(user)], check=True
    )
    (user / "a").write_text("client change", encoding="utf-8")
    git = LocalGit(user)
    git("add", "a")
    monkeypatch.chdir(user)
    result = CliRunner().invoke(
        main,
        [
            "member",
            "git",
            "publish",
            "--person",
            "aiko",
            "--repo-path",
            str(user),
            "--workspace-mode",
            "current",
            "--content-stdin",
        ],
        input="Publish through API",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pushed"]
    assert (
        payload["commit_sha"] == git("rev-parse", "origin/main").stdout.decode().strip()
    )
    remote = subprocess.run(
        ["git", "--git-dir", str(store.bare(REPOSITORY)), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remote.stdout.strip() == payload["commit_sha"]
    assert (
        git("show", "-s", "--format=%ae", "HEAD").stdout.strip()
        == b"aiko@guildbotics.local"
    )


def test_post_is_not_retried_after_disconnect():
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.RemoteProtocolError("Disconnected after write")

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            member_client._exchange(client, "/member/run", {}, io.BytesIO())
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_api_retains_workspace_until_a_cancelled_write_finishes(
    connected, monkeypatch
):
    from guildbotics.app_api.errors import AppApiError

    _, runtime, workspace_id = connected
    api = transport.MemberApi(runtime)
    started, release = threading.Event(), threading.Event()

    def run(arguments, invocation, **kwargs):
        assert invocation.run_id == invocation.task_run_id == ""
        assert invocation.lease is invocation.guest is invocation.work is None
        started.set()
        assert release.wait(5)
        return 0, "done", ""

    monkeypatch.setattr(transport, "run_in_process", run)
    with member_invocation_scope(MemberInvocation(task_run_id="unrelated-parent")):
        task = asyncio.create_task(
            api.execute(transport.MemberRequest(**request(workspace_id, ["help"])))
        )
    assert await asyncio.to_thread(started.wait, 5)
    assert runtime._accepted_command_ids
    with pytest.raises(AppApiError, match="workspace"):
        runtime.set_workspace(Path.cwd())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert runtime._accepted_command_ids
    closed = asyncio.create_task(api.close())
    await asyncio.sleep(0)
    assert not closed.done()
    release.set()
    await closed
    assert not runtime._accepted_command_ids


@pytest.mark.asyncio
async def test_worker_cleans_context_and_waits_for_cancelled_write():
    workers = MemberWorkers()
    leaked = contextvars.ContextVar("must-not-leak", default="clean")
    leaked.set("caller")
    started, release = threading.Event(), threading.Event()
    done = []

    def run():
        async def work():
            assert leaked.get() == "clean"
            started.set()
            assert release.wait(5)
            done.append("write")
            return 0, "ok", ""

        return asyncio.run(work())

    task = asyncio.create_task(workers.run(run, __import__("time").monotonic() + 10))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    close = asyncio.create_task(workers.settle())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert done == ["write"]
