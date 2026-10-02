"""Captured quota evidence and its clock-relative provider replay."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

QUOTA_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "antigravity_quota_1_2_13.json").read_text(
        encoding="utf-8"
    )
)


def quota_response(now: datetime) -> dict:
    """Keep the captured delay in the future without changing the raw evidence."""
    response = deepcopy(QUOTA_FIXTURE["upstream"])
    details = {item["@type"]: item for item in response["error"]["details"]}
    delay = details["type.googleapis.com/google.rpc.RetryInfo"]["retryDelay"]
    reset = now + timedelta(seconds=float(delay.removesuffix("s")))
    metadata = details["type.googleapis.com/google.rpc.ErrorInfo"]["metadata"]
    metadata["quotaResetTimeStamp"] = reset.isoformat().replace("+00:00", "Z")
    return response
