"""Sample /tmp usage for the rust-test capacity check (#447)."""

import json
import os
import subprocess
import sys
from pathlib import Path

SAMPLE_INTERVAL_MS = 100


def used_bytes(sample: os.statvfs_result) -> int:
    return (sample.f_blocks - sample.f_bfree) * sample.f_frsize


def measure(report: Path, args: list[str]) -> int:
    start = os.statvfs("/tmp")
    peak = used_bytes(start)
    with subprocess.Popen(args) as command:
        while True:
            peak = max(peak, used_bytes(os.statvfs("/tmp")))
            try:
                status = command.wait(timeout=SAMPLE_INTERVAL_MS / 1000)
            except subprocess.TimeoutExpired:
                continue
            break
    peak = max(peak, used_bytes(os.statvfs("/tmp")))
    result = json.dumps(
        {
            "path": "/tmp",
            "capacity_bytes": start.f_blocks * start.f_frsize,
            "start_used_bytes": used_bytes(start),
            "peak_used_bytes": peak,
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
        }
    )
    print(f"rust-test tmp usage: {result}", flush=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(result + "\n", encoding="utf-8")
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    sys.exit(measure(Path(sys.argv[1]), sys.argv[2:]))
