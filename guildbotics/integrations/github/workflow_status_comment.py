"""The workflow status a GitHub comment carries, as a hidden
``guildbotics-workflow-status-v1`` HTML comment in its body.

This module does **not** call the GitHub API; it only builds and parses
markdown strings.
"""

from __future__ import annotations

import json
import re

from guildbotics.runtime.port import WorkflowStatus

WORKFLOW_STATUS_MARKER = "guildbotics-workflow-status-v1"

_COMMENT_PATTERN = re.compile(
    r"<!--\s*" + re.escape(WORKFLOW_STATUS_MARKER) + r"\s*\n(.*?)\n\s*-->",
    re.DOTALL,
)


def render_workflow_status_comment(body: str, status: WorkflowStatus) -> str:
    """``body`` with the hidden marker of ``status`` before it."""
    json_line = json.dumps(
        status.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"<!-- {WORKFLOW_STATUS_MARKER}\n{json_line}\n-->\n\n{body}"


def parse_workflow_status_comment(body: str) -> WorkflowStatus | None:
    """The status a comment body carries; ``None`` when it carries none."""
    match = _COMMENT_PATTERN.search(body)
    if match is None:
        return None
    try:
        return WorkflowStatus.from_payload(json.loads(match.group(1).strip()))
    except ValueError:
        return None
