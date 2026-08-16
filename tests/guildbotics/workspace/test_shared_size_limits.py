"""A writer never accepts content the commit boundary will refuse to carry.

Size is the one class of failure the boundary catches that nothing else does.
A malformed file fails the product's own paths on every device; an oversized
one does not fail anywhere -- the save succeeds, the caller is told it
succeeded, and only the synchronization queue says, on every cycle from then
on, that the change cannot be sent. The screen that accepted it offers no way
to take it back, because nothing there knows a limit was crossed.

So each writer's limit is the boundary's own constant rather than a second
opinion about it. Restating the number is not a duplication that reads badly:
the two drift, and the gap between them is exactly the range where a save
succeeds and can never be shared.

The population is every size limit in the package, read out of the source, and
each one says whether it bounds a shared file or something else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from guildbotics.capabilities.member_memory import (
    MemberMemoryError,
    MemberMemoryService,
)
from guildbotics.entities.team import Person
from guildbotics.workspace.validation import (
    MAX_SHARED_FILE_BYTES,
    validate_shared_file,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "guildbotics"

#: The boundary's own limits. A writer's limit has to be one of these.
BOUNDARY_LIMITS = {
    "MAX_SHARED_AVATAR_BYTES",
    "MAX_SHARED_FILE_BYTES",
    "MAX_SHARED_JOURNAL_BYTES",
    "MAX_SHARED_TEXT_CHARS",
}

#: Limits on content that never reaches a shared file, and what each bounds.
NOT_SHARED = {
    "guildbotics/app_api/command_input_files.py:MAX_COMMAND_INPUT_FILE_BYTES": (
        "a temporary upload outside the workspace"
    ),
    "guildbotics/intelligences/agent_runtime/antigravity.py:_LOG_TAIL_BYTES": (
        "how much of an agent's log is read back after a failure"
    ),
    "guildbotics/intelligences/agent_runtime/antigravity.py:_MAX_PROMPT_BYTES": (
        "a prompt handed to an agent process"
    ),
    "guildbotics/intelligences/agent_runtime/member_broker.py:_MAX_ARGUMENT_BYTES": (
        "one argument of a broker request"
    ),
    "guildbotics/intelligences/agent_runtime/member_broker.py:_MAX_OUTPUT_BYTES": (
        "what the broker reads back from a member command"
    ),
    "guildbotics/intelligences/agent_runtime/member_broker.py:_MAX_REQUEST_BYTES": (
        "a broker request on the local socket"
    ),
    "guildbotics/intelligences/agent_runtime/member_broker.py:_MAX_STDIN_BYTES": (
        "what is piped into a member command"
    ),
    "guildbotics/observability/diagnostics_store.py:DEFAULT_DIAGNOSTICS_MAX_BYTES": (
        "local/run diagnostics, which stay on this device"
    ),
    "guildbotics/observability/diagnostics_store.py:_CURSOR_ANCHOR_BYTES": (
        "how far back a diagnostics cursor anchors when reading"
    ),
    "guildbotics/observability/session_transcripts.py:STANDARD_STDERR_TAIL_BYTES": (
        "how much of a command's stderr a transcript keeps"
    ),
}

_LIMIT_SUFFIXES = ("_BYTES", "_CHARS")


def _declared_limits() -> dict[str, ast.expr]:
    """Return every module-level size limit in the package and its value."""
    found: dict[str, ast.expr] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT.parent).as_posix()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if not target.id.endswith(_LIMIT_SUFFIXES):
                continue
            found[f"{relative}:{target.id}"] = node.value
    return found


def test_every_writer_limit_is_the_boundary_limit() -> None:
    """A new limit fails here until it says which side of the boundary it is on."""
    limits = _declared_limits()
    assert limits, "no size limits were found to check"

    unclassified: list[str] = []
    restated: list[str] = []
    for name, value in limits.items():
        if name.split(":")[1] in BOUNDARY_LIMITS:
            continue
        if name in NOT_SHARED:
            continue
        if isinstance(value, ast.Name) and value.id in BOUNDARY_LIMITS:
            continue
        if isinstance(value, ast.Constant | ast.BinOp):
            restated.append(name)
        else:
            unclassified.append(name)

    assert not restated, (
        "These bound something written to a shared file but state their own "
        f"number instead of the boundary's: {sorted(restated)}"
    )
    assert not unclassified, (
        f"These size limits say nothing about what they bound: {sorted(unclassified)}"
    )

    stale = sorted(set(NOT_SHARED) - set(limits))
    assert not stale, f"Excused as unshared but no longer declared: {stale}"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    return tmp_path


def test_a_memory_document_too_large_to_share_is_refused_when_recorded(
    workspace: Path,
) -> None:
    """The agent writing the body is told now, not by a queue it cannot see."""
    service = MemberMemoryService(Person(person_id="p1", name="P"))

    with pytest.raises(MemberMemoryError, match="above the"):
        service.record(
            scope="personal", title="t", body="x" * (MAX_SHARED_FILE_BYTES + 1)
        )

    documents = workspace / ".guildbotics/state/documents/personal/p1"
    assert not list(documents.glob("*/body.md"))


def test_a_refused_update_leaves_the_document_as_it_was(workspace: Path) -> None:
    """Metadata and body are measured together, so neither lands alone."""
    service = MemberMemoryService(Person(person_id="p1", name="P"))
    created = service.record(scope="personal", title="t", body="original")

    with pytest.raises(MemberMemoryError, match="above the"):
        service.update(doc_id=created["doc_id"], body="x" * (MAX_SHARED_FILE_BYTES + 1))

    document = service.get(doc_id=created["doc_id"])
    assert document["body"] == "original"
    assert document["meta"]["title"] == "t"


def test_what_the_writer_accepts_the_boundary_carries(workspace: Path) -> None:
    """The two limits are checked against each other, not just against a number."""
    service = MemberMemoryService(Person(person_id="p1", name="P"))
    created = service.record(scope="personal", title="t", body="y" * 900_000)

    root = workspace / ".guildbotics"
    for path in sorted((root / "state/documents/personal/p1").rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            validate_shared_file(relative, path.read_bytes())

    assert created["doc_id"]
