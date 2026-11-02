"""Language-neutral examples exercised against both sides of the wire."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from guildbotics.environment import command_environment, member_broker, worktree
from guildbotics.environment.spec import WORKTREE_SOURCE, WorktreeCopy
from guildbotics.guest.host_client import _result
from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.agent_runtime.wire import (
    CommandRequest,
    HostCallError,
    admits,
)
from guildbotics.runtime.member_invocation import GuestResult

GOLDEN = Path(__file__).resolve().parents[2] / "wire" / "golden"


def _load(name):
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


HTTP = _load("http")
ENTRY = _load("entry")


@pytest.mark.parametrize("case", HTTP, ids=lambda case: case["id"])
def test_http_server_and_client(case):
    """Only the host operation is substituted; the HTTP boundary is real."""
    broker = member_broker.MemberCapabilityBroker(lambda *a, **kw: None)
    broker._token = "golden-dummy"
    action = case["action"]
    calls = []

    async def answer(name, arguments):
        calls.append((name, arguments))
        if "error" in action:
            error = action["error"]
            if error["type"] == "AgentRuntimeError":
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory(error["category"]),
                    error["message"],
                    details=error["details"],
                )
            raise HostCallError(error["category"], error["message"], error["details"])
        if action.get("oversized"):
            return "x" * member_broker._MAX_OUTPUT_BYTES
        return action["result"]

    broker._host = answer
    app = Starlette(routes=[Route("/host/{call}", broker.call_host, methods=["POST"])])
    request = case["request"]
    with TestClient(app) as client:
        kwargs = (
            {"json": request["json"]}
            if "json" in request
            else {"content": request["text"]}
        )
        response = client.request(
            request["method"], request["path"], headers=request["headers"], **kwargs
        )
    expected = case["response"]
    assert response.status_code == expected["status"]
    if "json" in expected:
        assert response.json() == expected["json"]
    else:
        assert response.text == expected["text"]
    if case["id"] in {"bad-bearer", "bad-host", "invalid-json"}:
        assert calls == []
    else:
        assert calls == [("probe", request["json"])]

    # Feed the stored response independently, so a matching drift on both sides fails.
    response = httpx.Response(
        expected["status"],
        **(
            {"json": expected["json"]}
            if "json" in expected
            else {"text": expected["text"]}
        ),
    )
    expected_client = case["client"]
    if "result" in expected_client:
        assert _result(response) == expected_client["result"]
    else:
        error = expected_client["error"]
        kind = (
            AgentRuntimeError if error["type"] == "AgentRuntimeError" else HostCallError
        )
        with pytest.raises(kind) as caught:
            _result(response)
        assert str(caught.value) == error["message"]
        assert caught.value.category == error["category"]
        assert caught.value.details == error["details"]


def test_http_fixture_covers_every_category():
    errors = [case["client"]["error"] for case in HTTP if "error" in case["client"]]
    assert {e["category"] for e in errors} == {
        "refused",
        "unavailable",
        "failed",
        *AgentRuntimeErrorCategory,
    }
    assert {e["category"] for e in errors if e["type"] == "AgentRuntimeError"} == set(
        AgentRuntimeErrorCategory
    )


# Run main in a subprocess: it redirects OS descriptors, including those inherited
# by children. Substitute command execution, not the entry's output machinery.
_ENTRY_DRIVER = """
import json, subprocess, sys
from guildbotics.guest import entry
from guildbotics.intelligences.agent_runtime.wire import CommandReply
case = json.loads(sys.argv[1])
async def run(request, facts, client):
    assert request.model_dump(mode="json") == {
        **case["request"], "pipe": "", "invocation": None
    }
    print(case["stderr_lines"][0], flush=True)
    subprocess.run(
        [sys.executable, "-c", "print(" + repr(case["stderr_lines"][1]) + ")"],
        check=True,
    )
    return CommandReply.model_validate(case["reply"])
entry.run = run
entry.command_window = lambda: object()
entry.CommandFacts.read = lambda env: object()
sys.argv = ["entry"]
entry.main()
"""


@pytest.mark.parametrize("case", ENTRY, ids=lambda case: case["id"])
def test_entry_writes_only_reply_to_stdout(case):
    done = subprocess.run(
        [sys.executable, "-c", _ENTRY_DRIVER, json.dumps(case)],
        input=json.dumps(case["request"]).encode(),
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert done.stdout == case["stdout"].encode()
    assert done.stderr.decode().splitlines() == case["stderr_lines"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ENTRY, ids=lambda case: case["id"])
async def test_host_execute_reads_fixture_reply(case):
    data = case["stdout"].encode()
    # Multiple reads are necessary even when JSON contains escaped newlines.
    stream = SimpleNamespace(read=AsyncMock(side_effect=[data[:7], data[7:], b""]))
    process = SimpleNamespace(
        stdout=stream,
        stderr=asyncio.StreamReader(),
        stdin=SimpleNamespace(
            write=lambda data: None, drain=AsyncMock(), close=lambda: None
        ),
        wait=AsyncMock(return_value=0),
        kill=AsyncMock(),
    )
    process.stderr.feed_eof()
    environment = SimpleNamespace(run=AsyncMock(return_value=process))
    shared = object.__new__(command_environment._SharedEnvironment)
    shared._environment = environment
    shared._worktree = None
    reply = await shared.execute(CommandRequest.model_validate(case["request"]))
    assert reply.model_dump(mode="json") == case["reply"]
    assert stream.read.await_count == 2


@pytest.mark.parametrize("case", _load("admits"), ids=lambda case: case["id"])
def test_mount_admission(case):
    assert admits(case["mounts"], case["cwd"]) is case["expected"]


def _write_files(root, files):
    for file in files:
        path = root / file["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file["text"].encode())
        path.chmod(0o755 if file["executable"] else 0o644)


def _jsonl(text):
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.asyncio
async def test_worktree_producer_and_host_consumers(
    tmp_path, monkeypatch, posix_permissions
):
    from guildbotics.guest import worktree_copy

    case = _load("worktree")
    source, destination = tmp_path / "source", tmp_path / "destination"
    _write_files(source, case["before"])
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    argv = lambda operation: [
        arg.replace("{source}", str(source)).replace("{destination}", str(destination))
        for arg in case["argv"][operation]
    ]
    assert worktree_copy.main(argv("copy")) == 0
    assert _jsonl(sys.stdout.getvalue()) == _jsonl(case["copied_jsonl"])
    assert not (destination / "mounted").exists()
    assert not (destination / ".GIT").exists()

    _write_files(destination, case["after"])
    for name in case["delete"]:
        (destination / name).unlink()
    monkeypatch.setattr(sys, "stdin", io.StringIO(case["copied_jsonl"]))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert worktree_copy.main(argv("changes")) == 0
    assert _jsonl(sys.stdout.getvalue()) == _jsonl(case["changes_jsonl"])

    # The host sees a remote process only. Assert its argv and stdin, and return
    # the fixed JSONL rather than regenerating expected values from Python types.
    class Guest:
        def python(self, module, *args):
            assert module == "guildbotics.guest.worktree_copy"
            return list(args)

        async def run_here(
            self, args, *, cwd, env, stdout_limit, stdin=b"", stdout=None
        ):
            operation = args[0]
            expected = case["argv"][operation]
            assert args == [
                arg.replace("{source}", WORKTREE_SOURCE).replace(
                    "{destination}", "/work"
                )
                for arg in expected
            ]
            assert cwd == "/" and env == {}
            if operation == "copy":
                return GuestResult(0, case["copied_jsonl"].encode(), b"")
            assert _jsonl(stdin.decode()) == _jsonl(case["copied_jsonl"])
            stdout.write_text(case["changes_jsonl"], encoding="utf-8")
            return GuestResult(0, b"", b"")

    monkeypatch.setattr(worktree, "_guest", lambda _: Guest())
    copied = await worktree.copy_worktree(
        object(), WorktreeCopy(source, (0, 0), ("mounted",)), "/work"
    )
    assert list(copied.copied.values()) == _jsonl(case["copied_jsonl"])
    observed = []

    def consume(work, path, cancelled):
        observed.extend(worktree._changes(path, work.copy.excluded, True))

    monkeypatch.setattr(worktree, "_write", consume)
    await worktree.write_back(object(), copied)
    assert observed == _jsonl(case["changes_jsonl"])
