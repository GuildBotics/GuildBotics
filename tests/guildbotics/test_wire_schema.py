"""Pin the generated host/guest surface and its drift checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _assert_required_tags(schema):
    unions = [node for node in _nodes(schema) if "discriminator" in node]
    for union in unions:
        tag = union["discriminator"]["propertyName"]
        for value, ref in union["discriminator"]["mapping"].items():
            variant = schema
            for part in ref.removeprefix("#/").split("/"):
                variant = variant[part.replace("~1", "/").replace("~0", "~")]
            assert tag in variant["required"]
            assert variant["properties"][tag]["const"] == value
    return len(unions)


def test_discriminators_survive_in_every_generated_schema(generator):
    schemas = {
        name: json.loads(body) for name, body in generator.build_schemas().items()
    }
    entry = schemas["Entry.json"]
    items = schemas["RecordArguments.json"]["properties"]["entries"]["items"]
    assert items["discriminator"] == entry["discriminator"]
    assert items["oneOf"] == entry["oneOf"]
    # Walk all schemas, including definitions and containers, not only Entry.
    sites = {name for name, schema in schemas.items() if _assert_required_tags(schema)}
    assert {"Entry.json", "RecordArguments.json"} <= sites


@pytest.mark.parametrize("tag", ["event", "io", "summary", "credential"])
def test_entry_tags_match_host_validation(generator, tag):
    data = {
        "event": {
            "conversation": {
                "person_id": "aiko",
                "adapter": "codex",
                "work_kind": "ticket",
                "work_identity": "103",
            },
            "generation": 0,
            "event": {"kind": "turn", "name": "started"},
        },
        "io": {"io_type": "cli_agent.request", "payload": {}},
        "summary": {
            "slot": "default",
            "tool": "codex",
            "model_specified": False,
            "status": "finished",
        },
        "credential": {"tool": "codex", "failed": False},
    }[tag] | {"span": None}
    adapter = TypeAdapter(generator.wire.Entry)
    assert adapter.validate_python(data | {"type": tag}).type == tag
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        adapter.validate_python(data)
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        adapter.validate_python(data | {"type": "unknown"})
    entry = json.loads(generator.build_schemas()["Entry.json"])
    ref = entry["discriminator"]["mapping"][tag].split("/")[-1]
    assert "type" in entry["$defs"][ref]["required"]
    # Standalone constructors keep their default tags.
    model = getattr(generator.wire, ref)
    assert model.model_validate(data).type == tag
    standalone = json.loads(generator.build_schemas()[f"{ref}.json"])
    assert "type" not in standalone["required"]


def test_nested_tagged_unions_keep_other_defaults(generator):
    class First(BaseModel):
        kind: Literal["first"] = "first"
        text: str = ""

    class Second(BaseModel):
        kind: Literal["second"] = "second"
        count: int = 0

    union = Annotated[First | Second, Field(discriminator="kind")]
    schema = TypeAdapter(list[dict[str, union]]).json_schema(
        schema_generator=generator.WireJsonSchema
    )
    assert _assert_required_tags(schema) == 1
    for variant in schema["$defs"].values():
        assert variant["required"] == ["kind"]


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
        class CopiedFile(TypedDict):
            path: str
            new_field: int

        monkeypatch.setattr(
            generator,
            "TYPES",
            tuple(
                CopiedFile if typ is generator.wire.CopiedFile else typ
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
