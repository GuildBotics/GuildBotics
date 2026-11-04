#!/usr/bin/env python
"""Export the Python host/guest contracts as deterministic JSON Schema.

Run with --check in CI; regenerate and commit wire/schema after type changes.
Host argument objects are declared on the guest's shared wire surface.
The explicit call inventory makes a new host call require a schema decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from guildbotics.commands.authoring import CommandAuthoringResult
from guildbotics.intelligences.agent_runtime import wire
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionResult,
    ConversationKey,
    ConversationRecord,
)
from guildbotics.intelligences.brains.inference import AgnoAnswer, AgnoCall, JevCall
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from guildbotics.runtime.chat_service import (
    ChatChannelOutput,
    ChatIdentityOutput,
    ChatPostOutput,
    ChatReactionOutput,
)
from guildbotics.runtime.member_invocation import ChatSubject, Work
from guildbotics.runtime.resources import ReadPage
from guildbotics.runtime.workflow_invocation import ChatTurn, WorkflowInvocation

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "wire" / "schema"
REGENERATE_COMMAND = "uv run --no-sync python scripts/generate-wire-schema.py"

# Some host methods return dictionaries after serializing these concrete types.
# Keep the wire result, rather than the method's broad dict annotation.
HOST_RESULTS = {
    "acquire_inference": type(None),
    "begin_turn": wire.HostTurn,
    "end_turn": wire.EndTurnResult,
    "require_completion": type(None),
    "evidence": list[dict[str, object]],
    "record_completed": type(None),
    "record_completion_missing": type(None),
    "resolve": ConversationRecord,
    "save": ConversationRecord,
    "mark_unhealthy": ConversationRecord,
    "record": type(None),
    "member": wire.MemberCommandResult,
    "agno": AgnoAnswer,
    "jev": dict[str, object],
}

TYPES = (
    wire.ResolvedGuest,
    wire.ResolvedBrain,
    wire.CommandFacts,
    wire.CommandAccess,
    wire.CommandRequest,
    wire.CommandReply,
    wire.CommandFailure,
    wire.EventEntry,
    wire.IoEntry,
    wire.SummaryEntry,
    wire.CredentialEntry,
    wire.HostTurn,
    wire.CopiedFile,
    wire.ChangedFile,
    wire.EndTurnResult,
    WorkflowInvocation,
    Work,
    ChatSubject,
    ChatTurn,
    CliAgentExecutionResult,
    CommandAuthoringResult,
    TroubleshootingResult,
    AgnoCall,
    AgnoAnswer,
    JevCall,
    wire.MemberCommandResult,
    ConversationKey,
    ConversationRecord,
    ReadPage,
    ChatIdentityOutput,
    ChatPostOutput,
    ChatReactionOutput,
    ChatChannelOutput,
    wire.Unpacked,
)


class WireJsonSchema(GenerateJsonSchema):
    """Make tags mandatory where a discriminated union needs them.

    A model alone may default its tag, but a union cannot select a model
    without that tag. Keep the original Python defaults unchanged.
    """

    def tagged_union_schema(self, schema):
        result = super().tagged_union_schema(schema)
        discriminator = result.get("discriminator")
        if discriminator:
            tag = discriminator["propertyName"]
            for choice in result["oneOf"]:
                variant = self.resolve_ref_schema(choice)
                required = variant.setdefault("required", [])
                if tag not in required:
                    required.append(tag)
        return result


def build_schemas() -> dict[str, str]:
    """Build one self-contained schema per named wire type and call side."""
    if set(HOST_RESULTS) != set(wire.HOST_ARGUMENTS):
        raise ValueError("Host calls changed; update HOST_RESULTS and wire schemas.")
    types = {item.__name__: item for item in TYPES}
    types["Entry"] = wire.Entry
    for call, result in HOST_RESULTS.items():
        stem = "".join(part.capitalize() for part in call.split("_"))
        types[f"{stem}Arguments"] = wire.HOST_ARGUMENTS[call]
        types[f"{stem}Result"] = result
    return {
        f"{name}.json": json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **TypeAdapter(typ).json_schema(schema_generator=WireJsonSchema),
                "title": name,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, typ in sorted(types.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when committed schemas differ from Python types.",
    )
    check = parser.parse_args().check
    schemas = build_schemas()
    existing = {
        path.name: path.read_text(encoding="utf-8", newline="")
        for path in OUTPUT_PATH.glob("*.json")
    }
    if check:
        changed = sorted(
            name
            for name in schemas.keys() | existing.keys()
            if schemas.get(name) != existing.get(name)
        )
        if changed:
            print(
                f"Wire schemas out of date: {', '.join(changed)}\n"
                f"Regenerate with: {REGENERATE_COMMAND}",
                file=sys.stderr,
            )
            return 1
        print("Wire schemas are up to date.")
        return 0
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    for name in existing.keys() - schemas.keys():
        (OUTPUT_PATH / name).unlink()
    for name, content in schemas.items():
        (OUTPUT_PATH / name).write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {len(schemas)} wire schemas to {OUTPUT_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
