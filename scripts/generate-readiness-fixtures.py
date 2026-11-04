"""Execute the frozen host's readiness policy without a provider or network.

Run with uv run --no-sync python scripts/generate-readiness-fixtures.py.
"""

import importlib.util
import itertools
import json
import runpy
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = runpy.run_path(str(root / "scripts/generate-port-fixtures.py"))
source = subprocess.check_output(
    [
        "git",
        "show",
        f"{env['FROZEN_HOST']}:guildbotics/integrations/readiness.py",
    ],
    cwd=root,
    text=True,
)
spec = importlib.util.spec_from_loader("frozen_readiness", loader=None)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
exec(source, module.__dict__)
target = {"kind": "pull_request", "url": "local:pr/1", "title": "PR 🦀", "label": "!1"}
checks = [[]]
for status, conclusion in itertools.product(
    ("queued", "in_progress", "completed"),
    (
        None,
        "success",
        "neutral",
        "skipped",
        "failure",
        "error",
        "cancelled",
        "stale",
        "timed_out",
        "action_required",
        "startup_failure",
        "unknown",
    ),
):
    checks.append(
        [{"name": "ci", "status": status, "conclusion": conclusion, "details_url": ""}]
    )
checks.append(
    [
        {
            "name": "failure",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "",
        },
        {"name": "pending", "status": "queued", "conclusion": None, "details_url": ""},
    ]
)
cases = []
for (
    check,
    expected,
    changed_head,
    behind,
    changed_base,
    requires,
    closed,
) in itertools.product(
    checks,
    (False, True),
    (False, True),
    (0, 2),
    (False, True),
    (False, True),
    (False, True),
):
    freshness = (
        None
        if closed
        else {
            "base_sha": "base",
            "behind_by": behind,
            "current_base_sha": "new-base" if changed_base else "base",
            "requires_up_to_date": requires,
        }
    )
    params = {
        "target": target,
        "head_sha": "head",
        "current_head_sha": "new-head" if changed_head else "head",
        "checks": check,
        "checks_expected": expected,
        "freshness": freshness,
        "failed_logs": None,
    }
    args = {
        **params,
        "target": env["res"].WorkTarget.model_validate(target),
        "freshness": module.Freshness(**freshness) if freshness else None,
    }
    result = module.readiness_of(**args).model_dump(mode="json")
    cases.append({"input": params, "expected": result})
directory = root / "crates/gb-integrations/tests/fixtures"
directory.mkdir(parents=True, exist_ok=True)
(directory / "readiness.json").write_text(
    "[\n" + ",\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n]\n",
    encoding="utf-8",
)
print(f"{len(cases)} readiness cases")

log_cases = []
res = env["res"]
for text, repeats in [
    ("", 1),
    ("short 🦀\n", 1),
    ('\x7f"\\\n\x00日本🦀', 70000),
    ("ascii", 150000),
]:
    for already_truncated in (False, True):
        tail = text * repeats
        log = {
            "job_id": "1",
            "name": "ci",
            "conclusion": "failure",
            "url": "",
            "artifact_names": ["trace 🦀"],
            "log": tail,
            "log_bytes": 0,
            "tail_limit_bytes": res.MAX_LOG_TAIL_BYTES,
            "truncated": already_truncated,
        }
        args = {
            "target": res.WorkTarget.model_validate(target),
            "head_sha": "head",
            "current_head_sha": "head",
            "checks": [],
            "checks_expected": False,
            "freshness": None,
            "failed_logs": [dict(log), dict(log)],
        }
        expected = module.readiness_of(**args).model_dump(mode="json")
        lengths = [len(item["log"]) for item in expected["failed_logs"]]
        page_bytes = len(
            json.dumps(
                {"items": [expected], "continuation": None, "target": target}
            ).encode()
        )
        for item in expected["failed_logs"]:
            item["log"] = ""
        log_cases.append(
            {
                "text": text,
                "repeats": repeats,
                "already_truncated": already_truncated,
                "lengths": lengths,
                "expected": expected,
                "page_bytes": page_bytes,
            }
        )
(directory / "logs.json").write_text(
    json.dumps(log_cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
