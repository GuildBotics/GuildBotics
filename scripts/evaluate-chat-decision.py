#!/usr/bin/env python
"""Replay saved chat judgments inside a command VM, without chat actions."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from guildbotics.intelligences.decisions.assessment import Recorder, assess
from guildbotics.intelligences.decisions.chat_policy import QUESTION_VERSION, QUESTIONS
from guildbotics.intelligences.decisions.models import DecisionConfig, Question
from guildbotics.runtime.brain_factory import BrainFactory


def replay_context(person: str, workspace: Path) -> tuple[BrainFactory, Recorder]:
    """Use the command's resolved configuration and authenticated inference."""
    from guildbotics.guest.decisions import DecisionRecorder
    from guildbotics.guest.host_client import command_window
    from guildbotics.guest.inference import GuestInference
    from guildbotics.guest.resolved import ResolvedBrainFactory
    from guildbotics.intelligences.agent_runtime.wire import (
        RESOLVED_GUEST_ENV,
        CommandFacts,
        ResolvedGuest,
        admits,
    )
    from guildbotics.intelligences.brains.inference import install_inference

    client = command_window()
    if client is None:
        raise RuntimeError("Run this script inside a GuildBotics command environment.")
    facts = CommandFacts.read(os.environ)
    if facts.person_id != person or not admits(facts.mounts, str(workspace.resolve())):
        raise ValueError("Replay person or workspace is outside this command.")
    resolved = ResolvedGuest.model_validate_json(
        Path(os.environ[RESOLVED_GUEST_ENV]).read_bytes()
    )
    if resolved.member.get("person_id") != person:
        raise ValueError("Replay person differs from the resolved member.")
    install_inference(GuestInference(resolved, client))
    return ResolvedBrainFactory(resolved), DecisionRecorder(client)


class Capture:
    """Keep report metadata only after the required host write succeeds."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.records: dict[str, dict[str, Any]] = {}

    def io(self, record_id: str, payload: dict[str, Any]) -> None:
        self.recorder.io(record_id, payload)
        if "selection" in payload:
            self.records[record_id] = {
                key: payload[key]
                for key in (
                    "input_hash",
                    "question_version",
                    "rule_version",
                    "adoption_version",
                    "duration_ms",
                    "config",
                    "result",
                )
            }

    def evaluated(self, person_id: str, payload: dict[str, Any]) -> None:
        self.recorder.evaluated(person_id, payload)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--person", required=True)
    parser.add_argument("--brain", default="chat_decision")
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    questions = QUESTIONS
    if "payload" in source:
        original = source["payload"]
        if original.get("question_version") != QUESTION_VERSION:
            parser.error(
                "Saved questions use an older schema; use the current case set."
            )
        cases = [
            {
                "id": original.get("evaluation_id"),
                "state": original["input"]["state"],
                "original_selection": original.get("selection"),
            }
        ]
        questions = {
            key: Question.model_validate(q)
            for key, q in original["input"]["questions"].items()
        }
    else:
        cases = [
            {**case, "state": {**source["state"], **case["state"]}}
            for case in source["cases"]
        ]
    factory, destination = replay_context(args.person, args.workspace)
    recorder = Capture(destination)
    report = []
    for case in cases:
        selection, evaluation_id = await assess(
            case["state"],
            DecisionConfig(brain=args.brain),
            person_id=args.person,
            logger=logging.getLogger("decision-replay"),
            questions=questions,
            brain_factory=factory,
            recorder=recorder,
        )
        item = {
            "case": case["id"],
            "evaluation_id": evaluation_id,
            "selection": selection.model_dump(),
            **recorder.records.get(evaluation_id, {}),
        }
        if "expected_routes" in case:
            item["matches_expectation"] = selection.route in case["expected_routes"]
        if "original_selection" in case:
            item["original_selection"] = case["original_selection"]
        report.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if args.report:
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    asyncio.run(main())
