"""Language-neutral examples exercised against both sides of the wire."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import httpx
import pytest

from guildbotics.guest.host_client import _result
from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.agent_runtime.wire import (
    HostCallError,
    admits,
)

GOLDEN = Path(__file__).resolve().parents[2] / "wire" / "golden"


def _load(name):
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


HTTP = _load("http")
ENTRY = _load("entry")


@pytest.mark.parametrize("case", HTTP, ids=lambda case: case["id"])
def test_http_client_from_fixture(case):
    """Only the host operation is substituted; the HTTP boundary is real."""
    expected = case["response"]
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


@pytest.mark.parametrize("case", _load("admits"), ids=lambda case: case["id"])
def test_mount_admission(case):
    assert admits(case["mounts"], case["cwd"]) is case["expected"]


def test_mount_fixture_excludes_parent_traversal():
    """Do not require ports to reproduce behavior outside caller guarantees."""
    for case in _load("admits"):
        for path in [case["cwd"], *case["mounts"]]:
            assert ".." not in PurePosixPath(path).parts, (case["id"], path)


def _write_files(root, files):
    for file in files:
        path = root / file["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file["text"].encode())
        path.chmod(0o755 if file["executable"] else 0o644)


def _jsonl(text):
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.asyncio
async def test_worktree_producer_and_host_consumers(tmp_path, monkeypatch):
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
