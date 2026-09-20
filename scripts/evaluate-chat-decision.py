#!/usr/bin/env python
"""Replay a saved judgment input without chat actions or receipt updates."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from guildbotics.intelligences.decisions.assessment import assess
from guildbotics.intelligences.decisions.chat_policy import QUESTIONS
from guildbotics.intelligences.decisions.models import DecisionConfig, Question
from guildbotics.utils.fileio import get_workspace_config_dir, get_workspace_local_path
from guildbotics.utils.workspace_state import apply_workspace_for_cli


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="Saved required-io JSON or a case set with state and cases",
    )
    parser.add_argument(
        "--report", type=Path, help="Write the comparison report to this JSON file"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--person", required=True)
    parser.add_argument(
        "--brain", default="chat_decision", help="Configured brain feature"
    )
    args = parser.parse_args()
    apply_workspace_for_cli(args.workspace)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    if "payload" in source:
        original = source["payload"]
        request = original["input"]
        cases = [
            {
                "id": original.get("evaluation_id"),
                "state": request["state"],
                "original_selection": original.get("selection"),
            }
        ]
        questions = {
            key: Question.model_validate(q) for key, q in request["questions"].items()
        }
    else:
        cases = [
            {**case, "state": {**source["state"], **case["state"]}}
            for case in source["cases"]
        ]
        questions = QUESTIONS
    report = []
    for case in cases:
        selection, evaluation_id = await assess(
            case["state"],
            DecisionConfig(brain=args.brain),
            config_dir=get_workspace_config_dir(),
            person_id=args.person,
            logger=logging.getLogger("decision-replay"),
            questions=questions,
        )
        item = {
            "case": case["id"],
            "evaluation_id": evaluation_id,
            "selection": selection.model_dump(),
        }
        record = json.loads(
            get_workspace_local_path(
                "run", "required-io", f"{evaluation_id}.json"
            ).read_text(encoding="utf-8")
        )["payload"]
        item.update(
            {
                key: record[key]
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
        )
        if "expected_routes" in case:
            item["matches_expectation"] = selection.route in case[
                "expected_routes"
            ] and (
                "expected_effort" not in case
                or selection.effort == case["expected_effort"]
            )
        if "original_selection" in case:
            item["original_selection"] = case["original_selection"]
        report.append(item)
        print(
            json.dumps(
                {key: value for key, value in item.items() if key != "result"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.report:
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    asyncio.run(main())
