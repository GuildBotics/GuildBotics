"""Translate GitHub read payloads without combining resources."""

import re
from typing import Any

from guildbotics.entities import Person
from guildbotics.integrations.github.github_utils import get_author_type

PATCH_HUNK_RE = re.compile(
    r"^@@ -(?P<left>\d+)(?:,(?P<left_count>\d+))? \+(?P<right>\d+)(?:,(?P<right_count>\d+))? @@"
)


def _comment_summary(person: Person, comment: dict[str, Any]) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    return {
        "id": comment.get("id"),
        "body": body,
        "author": login,
        "author_type": get_author_type(person, login) if login else "",
        "created_at": comment.get("created_at"),
        "html_url": comment.get("html_url"),
    }


def _review_summary(person: Person, review: dict[str, Any]) -> dict[str, Any]:
    body = str(review.get("body") or "")
    user = review.get("user") or {}
    login = str(user.get("login") or "")
    return {
        "id": review.get("id"),
        "body": body,
        "author": login,
        "author_type": get_author_type(person, login) if login else "",
        "state": review.get("state", ""),
        "submitted_at": review.get("submitted_at"),
        "html_url": review.get("html_url"),
        "commit_id": review.get("commit_id"),
    }


def _pull_request_file_summary(file: dict[str, Any]) -> dict[str, Any]:
    path = str(file.get("filename") or "")
    patch = str(file.get("patch") or "")
    coordinates = _commentable_lines_from_patch(path, patch)
    additions = sum(
        line["side"] == "RIGHT" and "left_line" not in line for line in coordinates
    )
    deletions = sum(line["side"] == "LEFT" for line in coordinates)
    return {
        "path": path,
        "status": file.get("status", ""),
        "additions": file.get("additions", 0),
        "deletions": file.get("deletions", 0),
        "changes": file.get("changes", 0),
        "patch": file.get("patch"),
        "patch_available": "patch" in file,
        "patch_complete": "patch" in file
        and additions == file.get("additions")
        and deletions == file.get("deletions"),
        "commentable_lines": coordinates,
    }


def _graphql_review_comment_summary(
    person: Person, comment: dict[str, Any]
) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    author = comment.get("author") or {}
    login = str(author.get("login") or "")
    return {
        "id": comment.get("databaseId"),
        "body": body,
        "author": login,
        "author_type": get_author_type(person, login) if login else "",
        "created_at": comment.get("createdAt"),
        "html_url": comment.get("url"),
    }


def _commentable_lines_from_patch(path: str, patch: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    left_line: int | None = None
    right_line: int | None = None
    for raw_line in patch.splitlines():
        hunk = PATCH_HUNK_RE.match(raw_line)
        if hunk:
            left_line = int(hunk.group("left"))
            right_line = int(hunk.group("right"))
            continue
        if left_line is None or right_line is None or raw_line.startswith("\\"):
            continue
        marker = raw_line[:1]
        content = raw_line[1:] if marker in {" ", "+", "-"} else raw_line
        if marker == " ":
            lines.append(
                {
                    "path": path,
                    "line": right_line,
                    "side": "RIGHT",
                    "left_line": left_line,
                    "right_line": right_line,
                    "content": content,
                }
            )
            left_line += 1
            right_line += 1
        elif marker == "+":
            lines.append(
                {
                    "path": path,
                    "line": right_line,
                    "side": "RIGHT",
                    "right_line": right_line,
                    "content": content,
                }
            )
            right_line += 1
        elif marker == "-":
            lines.append(
                {
                    "path": path,
                    "line": left_line,
                    "side": "LEFT",
                    "left_line": left_line,
                    "content": content,
                }
            )
            left_line += 1
    return lines


def _project_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    project = item.get("project") or {}
    field_values = []
    for value in ((item.get("fieldValues") or {}).get("nodes")) or []:
        field = value.get("field") or {}
        field_name = field.get("name")
        if not field_name:
            continue
        field_values.append(
            {
                "field": field_name,
                "value": next(
                    (
                        value[key]
                        for key in ("name", "text", "date", "number")
                        if value.get(key) is not None
                    ),
                    None,
                ),
            }
        )
    return {
        "item_id": item.get("id"),
        "project_title": project.get("title"),
        "project_number": project.get("number"),
        "project_url": project.get("url"),
        "fields": field_values,
    }
