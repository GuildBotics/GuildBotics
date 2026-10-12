"""A changed file of a pull request, with the lines of its unified diff an
inline comment can be put on, whichever code host made the diff."""

from __future__ import annotations

import re
from typing import Any

_HUNK = re.compile(r"^@@ -(?P<left>\d+)(?:,\d+)? \+(?P<right>\d+)(?:,\d+)? @@")


def file_change(
    path: str,
    status: str,
    patch: str | None,
    *,
    additions: int,
    deletions: int,
    changes: int,
) -> dict[str, Any]:
    """The file as the ports read it (``FileChange``).

    Args:
        path: The file's path.
        status: How it changed (``added``, ``modified``, ...).
        patch: Its hunks, without the file headers; ``None`` when the code
            host gives none (a binary or an oversized file).
        additions: The lines the code host counts as added.
        deletions: The lines the code host counts as deleted.
        changes: The lines the code host counts as changed.
    """
    coordinates = commentable_lines(patch or "")
    added = sum(
        line["side"] == "RIGHT" and "left_line" not in line for line in coordinates
    )
    deleted = sum(line["side"] == "LEFT" for line in coordinates)
    return {
        "path": path,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": changes,
        "patch": patch,
        "patch_available": patch is not None,
        "patch_complete": patch is not None
        and added == additions
        and deleted == deletions,
        "commentable_lines": coordinates,
    }


def commentable_lines(patch: str) -> list[dict[str, Any]]:
    """Each line of the hunks, on the side an inline comment names it by."""
    lines: list[dict[str, Any]] = []
    left_line: int | None = None
    right_line: int | None = None
    for raw_line in patch.splitlines():
        hunk = _HUNK.match(raw_line)
        if hunk:
            left_line = int(hunk.group("left"))
            right_line = int(hunk.group("right"))
            continue
        if left_line is None or right_line is None or raw_line.startswith("\\"):
            continue
        marker = raw_line[:1]
        if marker == " ":
            lines.append(
                {
                    "line": right_line,
                    "side": "RIGHT",
                    "left_line": left_line,
                    "right_line": right_line,
                }
            )
            left_line += 1
            right_line += 1
        elif marker == "+":
            lines.append(
                {"line": right_line, "side": "RIGHT", "right_line": right_line}
            )
            right_line += 1
        elif marker == "-":
            lines.append({"line": left_line, "side": "LEFT", "left_line": left_line})
            left_line += 1
    return lines
