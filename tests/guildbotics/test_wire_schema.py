"""Pin the generated host/guest surface and its drift checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def generator():
    spec = importlib.util.spec_from_file_location(
        "generate_wire_schema", ROOT / "scripts" / "generate-wire-schema.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_schemas_match_python_types(generator):
    committed = {
        path.name: path.read_text(encoding="utf-8", newline="")
        for path in generator.OUTPUT_PATH.glob("*.json")
    }
    assert committed == generator.build_schemas(), generator.REGENERATE_COMMAND


def test_host_calls_all_have_both_sides(generator):
    schemas = generator.build_schemas()
    assert set(generator.HOST_RESULTS) == generator._CALLS
    for call in generator._CALLS:
        stem = "".join(part.capitalize() for part in call.split("_"))
        assert f"{stem}Arguments.json" in schemas
        assert f"{stem}Result.json" in schemas
    begin = json.loads(schemas["BeginTurnArguments.json"])
    assert begin["required"] == ["tool", "cwd"]
    assert begin["properties"]["participant_labels"]["default"] == ""
    assert begin["additionalProperties"] is False
    entry = json.loads(schemas["Entry.json"])
    assert set(entry["discriminator"]["mapping"]) == {
        "event",
        "io",
        "summary",
        "credential",
    }
    changed = json.loads(schemas["ChangedFile.json"])
    assert changed["required"] == ["path"]


def test_named_wire_population(generator):
    # This inventory is deliberately independent of the exporter registry.
    expected = {
        "CommandFacts",
        "CommandAccess",
        "CommandRequest",
        "CommandReply",
        "CommandFailure",
        "EventEntry",
        "IoEntry",
        "SummaryEntry",
        "CredentialEntry",
        "Entry",
        "HostTurn",
        "CopiedFile",
        "ChangedFile",
        "WorkflowInvocation",
        "Work",
        "ChatSubject",
        "ChatTurn",
        "CliAgentExecutionResult",
        "CommandAuthoringResult",
        "TroubleshootingResult",
        "AgnoCall",
        "AgnoAnswer",
        "JevCall",
        "MemberCommandResult",
        "ConversationKey",
        "ConversationRecord",
        "ReadPage",
        "Unpacked",
        "ChatIdentityOutput",
        "ChatPostOutput",
        "ChatReactionOutput",
        "ChatChannelOutput",
    }
    assert {f"{name}.json" for name in expected} <= generator.build_schemas().keys()


def test_new_host_call_without_schema_is_rejected(generator, monkeypatch):
    monkeypatch.setattr(generator, "_CALLS", generator._CALLS | {"new_call"})
    with pytest.raises(ValueError, match="Host calls changed"):
        generator.build_schemas()


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "field"])
def test_check_rejects_schema_drift(generator, monkeypatch, tmp_path, damage):
    monkeypatch.setattr(generator, "OUTPUT_PATH", tmp_path)
    monkeypatch.setattr("sys.argv", ["generate-wire-schema.py"])
    assert generator.main() == 0
    path = tmp_path / "CopiedFile.json"
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_text("{}\n", encoding="utf-8")
    elif damage == "extra":
        (tmp_path / "Obsolete.json").write_text("{}\n", encoding="utf-8")
    else:
        # Change a source type while leaving the committed output untouched.
        copied = TypedDict("CopiedFile", {"path": str, "new_field": int})
        monkeypatch.setattr(
            generator,
            "TYPES",
            tuple(
                copied if typ is generator.wire.CopiedFile else typ
                for typ in generator.TYPES
            ),
        )
    monkeypatch.setattr("sys.argv", ["generate-wire-schema.py", "--check"])
    assert generator.main() == 1
    assert path.exists() == (damage != "missing")
    if damage == "field":
        assert json.loads(path.read_text())["properties"].get("new_field") is None
    monkeypatch.setattr("sys.argv", ["generate-wire-schema.py"])
    assert generator.main() == 0
    monkeypatch.setattr("sys.argv", ["generate-wire-schema.py", "--check"])
    assert generator.main() == 0
